"""关联解析器 —— 「飞书用户 ↔ IDC 账号」的分层解析。

设计依据 ../../docs/design/02-paradigm.md、05-migration-and-flow.md。

v2 与早期方案的本质区别：
- **运行态零探测**。日常登录/查询只读 DynamoDB 映射，绝不在热路径用拼音
  逐个 get_user_id 探测 IDC（早期方案每次登录最坏 12 次 AWS 调用）。
- **拼音猜测降级为迁移期兜底**。仅一次性迁移脚本才会走拼音探测，命中即写映射，
  此后转为纯 DB 读路径。
- **UserId 为锚点**，改名不影响（FeishuNameAlias 真名保护表因此退役）。

分层优先级（命中即停）：
    ① DB 映射（主路径，运行态唯一会走的）
    ② email 精确匹配（迁移期：飞书 OAuth 邮箱 == IDC 邮箱，最可靠）
    ③ 拼音候选探测（迁移期兜底）
    ④ 管理员手工关联（前三者失败）
"""
from __future__ import annotations

import logging
import re

from pypinyin import lazy_pinyin

from app.aws import get_identity_store_id, get_session
from app.config import settings
from app.mapping_store import (
    PRIMARY,
    SECONDARY,
    AccountMapping,
    MappingStore,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 姓名 → 拼音候选（仅迁移期 / 申请推荐用，运行态不依赖）
# ---------------------------------------------------------------------------

def main_name_pinyin(feishu_name: str) -> str:
    """取括号外主名的全拼。"张三(Sam)" → "zhangsan"。括号内别名忽略。"""
    main = re.sub(r"[（(].*?[）)]", "", feishu_name or "").strip()
    if not main:
        return ""
    return "".join(lazy_pinyin(main)).lower()


def link_candidates(feishu_name: str) -> list[str]:
    """关联存量账号的候选用户名（迁移期探测用）：base / base1~5 / base-new / base-new1~5。"""
    base = main_name_pinyin(feishu_name)
    if not base:
        return []
    out = [base]
    out += [f"{base}{i}" for i in range(1, 6)]
    out.append(f"{base}-new")
    out += [f"{base}-new{i}" for i in range(1, 6)]
    return out


def suggest_new_username(feishu_name: str, taken: set[str]) -> str:
    """申请新账号时推荐下一个可用 -new 用户名（仅建议默认值，猜错无害）。

    优先 base 本身，再 base-new1..5，跳过已占用。
    """
    base = main_name_pinyin(feishu_name)
    if not base:
        return ""
    for cand in [base] + [f"{base}-new{i}" for i in range(1, 6)]:
        if cand not in taken:
            return cand
    # 全占用则继续递增
    i = 6
    while f"{base}-new{i}" in taken:
        i += 1
    return f"{base}-new{i}"


# ---------------------------------------------------------------------------
# 运行态解析（只读 DB，零探测）
# ---------------------------------------------------------------------------

class Resolver:
    def __init__(self, store: MappingStore | None = None):
        self.store = store or MappingStore()

    def accounts_of(self, feishu_open_id: str) -> list[AccountMapping]:
        """运行态主路径：一个人的所有账号，纯 DB 读。"""
        return self.store.list_by_feishu(feishu_open_id)

    def record_new_account(
        self, kiro_user_id: str, feishu_open_id: str, feishu_name: str,
        kiro_username: str, kiro_email: str, tier: str, team: str,
        approved_by: str = "",
    ) -> AccountMapping:
        """新建账号开通成功后写映射。自动判定主/副：首个账号为主，其余为副。"""
        role = SECONDARY if self.store.has_primary(feishu_open_id) else PRIMARY
        mapping = AccountMapping(
            kiro_user_id=kiro_user_id,
            feishu_open_id=feishu_open_id,
            feishu_name=feishu_name,
            team=team,
            kiro_username=kiro_username,
            kiro_email=kiro_email,
            tier=tier,
            status="active",
            account_role=role,
            approved_by=approved_by,
        )
        self.store.put(mapping)
        logger.info("记录新账号映射: %s (%s) -> %s [%s]",
                    kiro_username, kiro_user_id, feishu_name, role)
        return mapping

    def manual_link(
        self, kiro_user_id: str, feishu_open_id: str, feishu_name: str = "",
        **extra,
    ) -> AccountMapping:
        """④ 管理员手工关联：直接建立 (open_id ↔ user_id) 映射。"""
        role = SECONDARY if self.store.has_primary(feishu_open_id) else PRIMARY
        mapping = AccountMapping(
            kiro_user_id=kiro_user_id, feishu_open_id=feishu_open_id,
            feishu_name=feishu_name, account_role=role, **extra,
        )
        self.store.put(mapping)
        return mapping

    def unlink(self, kiro_user_id: str) -> None:
        """解除关联：仅删本地映射，不动 IDC 账号。"""
        self.store.delete(kiro_user_id)


# ---------------------------------------------------------------------------
# 迁移期解析（email ▶ 拼音，命中即写映射；不在运行态调用）
# ---------------------------------------------------------------------------

class MigrationResolver:
    """一次性存量迁移用。把 IDC 存量账号关联到飞书用户并写映射。

    与 Resolver 分离，强调：拼音探测**只在这里**发生，运行态绝不触发。
    """

    def __init__(self, store: MappingStore | None = None):
        self.store = store or MappingStore()
        self._session = get_session()
        self._region = settings.aws_region
        self._id_store = get_identity_store_id(self._session)
        self._idc = self._session.client("identitystore", region_name=self._region)

    def _describe(self, user_id: str) -> dict | None:
        try:
            return self._idc.describe_user(IdentityStoreId=self._id_store, UserId=user_id)
        except Exception:
            return None

    def _user_id_by_name(self, username: str) -> str | None:
        try:
            return self._idc.get_user_id(
                IdentityStoreId=self._id_store,
                AlternateIdentifier={"UniqueAttribute": {
                    "AttributePath": "userName", "AttributeValue": username}},
            )["UserId"]
        except Exception:
            return None

    @staticmethod
    def _primary_email(idc_user: dict) -> str:
        emails = idc_user.get("Emails", []) or []
        primary = next((e for e in emails if e.get("Primary")), None)
        return (primary or (emails[0] if emails else {})).get("Value", "")

    def resolve_for_user(
        self, feishu_open_id: str, feishu_name: str, feishu_email: str = "",
    ) -> AccountMapping | None:
        """对单个飞书用户解析存量账号：② email 精确 ▶ ③ 拼音兜底。命中即写映射。

        返回新建的映射（或 None=未匹配，交 ④ 手工）。
        """
        # 已有映射则不重复（① 在运行态已覆盖，这里防迁移重复）
        existing = self.store.list_by_feishu(feishu_open_id)
        already = {m.kiro_username for m in existing}

        # ② email 精确匹配
        if feishu_email:
            uid = self._find_idc_user_by_email(feishu_email)
            if uid:
                return self._write_from_idc(uid, feishu_open_id, feishu_name)

        # ③ 拼音候选探测（仅迁移期）
        for cand in link_candidates(feishu_name):
            if cand in already:
                continue
            uid = self._user_id_by_name(cand)
            if uid:
                return self._write_from_idc(uid, feishu_open_id, feishu_name)

        return None

    def _find_idc_user_by_email(self, email: str) -> str | None:
        """ListUsers 不支持按 email filter（见设计 03），故全量扫一遍匹配 primary email。

        仅迁移期一次性执行，可接受。
        """
        paginator = self._idc.get_paginator("list_users")
        for page in paginator.paginate(IdentityStoreId=self._id_store):
            for u in page.get("Users", []):
                if self._primary_email(u).lower() == email.lower():
                    return u["UserId"]
        return None

    def _write_from_idc(self, user_id: str, feishu_open_id: str, feishu_name: str) -> AccountMapping:
        """读 IDC 用户详情，写入映射。主/副由是否已有主账号决定。"""
        u = self._describe(user_id) or {}
        role = SECONDARY if self.store.has_primary(feishu_open_id) else PRIMARY
        mapping = AccountMapping(
            kiro_user_id=user_id,
            feishu_open_id=feishu_open_id,
            feishu_name=feishu_name,
            kiro_username=u.get("UserName", ""),
            kiro_email=self._primary_email(u),
            account_role=role,
            status="active",
        )
        self.store.put(mapping)
        logger.info("迁移关联: %s -> %s [%s]", mapping.kiro_username, feishu_name, role)
        return mapping
