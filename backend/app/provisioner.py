# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kiro 账号开通 / 上限调整 / 停用 / 恢复 / 回收（积分池版）核心工作流。

开通（全步骤幂等，分步错误定位）：
  1) IAM Identity Center 建用户  2) 加组  3) 发密码设置邮件
  4) 加入积分池  5) 席位（预付池分配；后付池加入即自动占座）  6) 可选：设置个人用量上限
预付池没有空余席位时，开通仍算成功（用户已在池里，状态为「等待席位」），结果带 warning，
管理员在 Kiro 控制台买席位后用「重新分配席位」补上。

设计要点（安全基线）：凭证全部走 app.aws（IAM Role），无 AK/SK；
开通成功返回 IDC UserId（稳定锚点），供上层写 DynamoDB 映射。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from botocore.exceptions import ClientError

from app import kiro_api, pool
from app.aws import get_frozen_credentials, get_identity_store_id, get_session
from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ProvisionResult:
    success: bool
    user_id: str = ""
    error: str = ""
    error_step: str = ""
    warning: str = ""
    steps_succeeded: list = field(default_factory=list)


@dataclass
class SimpleResult:
    success: bool
    error: str = ""
    warning: str = ""


# ---------------------------------------------------------------------------
# IDC helper
# ---------------------------------------------------------------------------

def _idc():
    session = get_session()
    return session.client("identitystore", region_name=settings.aws_region), \
        get_identity_store_id(session)


def _create_user(idc, id_store, username, given_name, family_name, email) -> tuple[str, bool]:
    """创建 IDC 用户。同名已存在直接报错（防止认领他人账号）。返回 (user_id, created)。"""
    try:
        resp = idc.create_user(
            IdentityStoreId=id_store,
            UserName=username,
            Name={"GivenName": given_name or username, "FamilyName": family_name or username},
            DisplayName=f"{given_name} {family_name}".strip() or username,
            Emails=[{"Value": email, "Type": "Work", "Primary": True}],
        )
        return resp["UserId"], True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConflictException":
            raise RuntimeError(f"用户名 '{username}' 在 IDC 中已存在，请更换用户名") from exc
        raise


def _add_to_group(idc, id_store, user_id: str, group_name: str) -> bool:
    """加组。返回 True = 新加入，False = 已是组成员。"""
    gid = idc.get_group_id(
        IdentityStoreId=id_store,
        AlternateIdentifier={"UniqueAttribute": {
            "AttributePath": "displayName", "AttributeValue": group_name}},
    )["GroupId"]
    try:
        idc.create_group_membership(IdentityStoreId=id_store, GroupId=gid,
                                    MemberId={"UserId": user_id})
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConflictException":
            return False
        raise


def _seat_warning(reason: str) -> str:
    return pool.SEAT_REASON_TEXT.get(reason, f"席位分配未完成（{reason}），请在管理页重试")


# ---------------------------------------------------------------------------
# 开通
# ---------------------------------------------------------------------------

def provision(username: str, email: str, given_name: str, family_name: str,
              group_name: str, usage_limit: float | None = None) -> ProvisionResult:
    """开通 Kiro 账号并加入积分池。成功后 result.user_id 即稳定锚点。"""
    result = ProvisionResult(success=False)
    try:
        idc, id_store = _idc()
        creds = get_frozen_credentials()
    except Exception as exc:  # noqa: BLE001
        result.error, result.error_step = f"AWS 凭证 / IDC 不可用: {exc}", "init"
        return result

    steps = [
        ("create_user", lambda: _step_create_user(result, idc, id_store, username,
                                                  given_name, family_name, email)),
        ("add_to_group", lambda: result.steps_succeeded.append(
            "group_added" if _add_to_group(idc, id_store, result.user_id, group_name)
            else "group_already_member")),
        ("send_password", lambda: (kiro_api.send_password_email(result.user_id, credentials=creds),
                                   result.steps_succeeded.append("password_email_sent"))),
        ("join_pool", lambda: result.steps_succeeded.append(
            "pool_joined" if pool.add_member(result.user_id) else "pool_already_member")),
        ("assign_seat", lambda: _step_seat(result)),
    ]
    if usage_limit is not None:
        steps.append(("set_limit", lambda: (pool.set_limit("USER", result.user_id, usage_limit),
                                            result.steps_succeeded.append("limit_set"))))
    labels = {"create_user": "创建用户", "add_to_group": f"加入组 '{group_name}'",
              "send_password": "发送密码邮件", "join_pool": "加入积分池",
              "assign_seat": "分配席位", "set_limit": "设置用量上限"}
    for name, fn in steps:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 分步定位，原样带回错误
            msg = str(exc)
            result.error = msg if name == "create_user" else f"{labels[name]}失败: {msg}"
            result.error_step = name
            logger.warning("provision step %s failed: %s", name, msg)
            return result
    result.success = True
    return result


def _step_create_user(result, idc, id_store, username, given_name, family_name, email):
    user_id, created = _create_user(idc, id_store, username, given_name, family_name, email)
    result.user_id = user_id
    result.steps_succeeded.append("user_created" if created else "user_exists")


def _step_seat(result: ProvisionResult) -> None:
    ok, reason = pool.assign_seat(result.user_id)
    if ok:
        result.steps_succeeded.append("seat_auto" if reason == "AUTO" else "seat_assigned")
    else:
        result.steps_succeeded.append("awaiting_seat")
        result.warning = _seat_warning(reason)


# ---------------------------------------------------------------------------
# 上限 / 席位
# ---------------------------------------------------------------------------

def set_user_limit(user_id: str, limit: float | None) -> SimpleResult:
    """设置 / 删除个人用量上限（None = 删除，回落到组上限或 profile 默认）。"""
    try:
        pool.set_limit("USER", user_id, limit)
        return SimpleResult(success=True)
    except Exception as exc:  # noqa: BLE001
        return SimpleResult(success=False, error=str(exc))


def retry_seats(user_ids: list[str]) -> list[dict]:
    """预付池：对「等待席位」的账号重新分配席位（管理员在控制台买完席位后用）。"""
    results = []
    for uid in user_ids:
        try:
            ok, reason = pool.assign_seat(uid)
            results.append({"user_id": uid, "success": ok,
                            "error": "" if ok else _seat_warning(reason)})
        except Exception as exc:  # noqa: BLE001
            results.append({"user_id": uid, "success": False, "error": str(exc)})
    return results


# ---------------------------------------------------------------------------
# 停用 / 恢复 / 回收
# ---------------------------------------------------------------------------

def deactivate(user_ids: list[str]) -> list[dict]:
    """停用 = 移出积分池（即时生效），保留 IDC 用户与映射。

    已产生的 credits 不退；本账期的席位费照收，下个账期起不再计费。
    用户若还通过某个已加入池的组拿到访问，移出直接分配后仍可使用（见 live 实况）。
    """
    from app.mapping_store import CANCELLED, MappingStore

    mapping = MappingStore()
    results = []
    for uid in user_ids:
        try:
            pool.remove_member(uid)
            try:
                mapping.update_fields(uid, status=CANCELLED)
            except Exception:  # noqa: BLE001
                logger.warning("deactivate: mapping snapshot update failed for %s", uid)
            results.append({"user_id": uid, "success": True, "error": ""})
        except Exception as exc:  # noqa: BLE001
            results.append({"user_id": uid, "success": False, "error": str(exc)})
    return results


def reactivate(user_ids: list[str]) -> list[dict]:
    """恢复 = 重新加入积分池并分配席位。席位不足时成功但带 warning。"""
    from app.mapping_store import ACTIVE, MappingStore

    mapping = MappingStore()
    results = []
    for uid in user_ids:
        try:
            pool.add_member(uid)
            ok, reason = pool.assign_seat(uid)
            try:
                mapping.update_fields(uid, status=ACTIVE)
            except Exception:  # noqa: BLE001
                logger.warning("reactivate: mapping snapshot update failed for %s", uid)
            results.append({"user_id": uid, "success": True, "error": "",
                            "warning": "" if ok else _seat_warning(reason)})
        except Exception as exc:  # noqa: BLE001
            results.append({"user_id": uid, "success": False, "error": str(exc), "warning": ""})
    return results


def bulk_cancel(user_ids: list[str]) -> list[dict]:
    """回收 = 移出积分池 + 清个人上限 + 删 IDC 用户 + 删映射（副账号回收，不可逆）。

    四件事一起做，避免残留孤儿记录（IDC 已删但映射还在 → 界面出现幽灵账号）。
    """
    from app.mapping_store import MappingStore

    idc, id_store = _idc()
    mapping = MappingStore()
    personal_limits = set()
    try:
        personal_limits = {x["scope_id"] for x in pool.list_limits(force=True)
                           if x["scope"] == "USER"}
    except Exception:  # noqa: BLE001
        logger.warning("bulk_cancel: list_limits failed, skip limit cleanup")
    results = []
    for uid in user_ids:
        try:
            pool.remove_member(uid)
            if uid in personal_limits:
                try:
                    pool.set_limit("USER", uid, None)
                except Exception:  # noqa: BLE001
                    logger.warning("bulk_cancel: clear limit failed for %s", uid)
            idc.delete_user(IdentityStoreId=id_store, UserId=uid)
            mapping.delete(uid)
            results.append({"user_id": uid, "success": True, "error": ""})
        except Exception as exc:  # noqa: BLE001
            results.append({"user_id": uid, "success": False, "error": str(exc)})
    return results


# ---------------------------------------------------------------------------
# 实况
# ---------------------------------------------------------------------------

def live_status_map(force: bool = False) -> dict:
    """池内实况 {user_id: {"status": SEATED / AWAITING_SEAT, "direct": bool}}；不在 dict = 不在池里。

    映射表里的 status 是平台操作时的快照，管理员在控制台直接移出 / 加入不会回写映射表，以此为准。
    失败抛异常，由调用方降级。
    """
    return pool.member_status_map(force=force)
