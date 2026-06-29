# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB 映射层 —— 「飞书 ↔ IDC」唯一真相源。

设计依据见 ../../docs/design/06-data-layer.md。

核心模型：
- **唯一实体是 Kiro 账号（kiro_user_id），不是人**。一个 UserId 只属于一个人，
  一个人可有多个 UserId（1:N）。
- PK = kiro_user_id（IDC 稳定主键，永不随改名变化 —— 这是整个 v2 范式的锚点）
- GSI = feishu_open_id（反查「一个人的所有账号」）

字段：
    kiro_user_id   (PK)   IDC UserId
    feishu_open_id (GSI)  归属飞书用户
    feishu_name           展示
    team                  分组
    kiro_username         展示（退化为非关键字段）
    kiro_email
    tier                  pro / pro+ / power / ""
    status                active / cancelled
    account_role          primary / secondary   ★ 主副维度
    retention             permanent / monthly    ★ 由 role 推导
    approved_by           审批人 open_id（审计）
    created_at / updated_at

运行态只读写本表，**不在热路径探测 IDC**（避免每次登录拼音探测 IDC）。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from boto3.dynamodb.conditions import Key

from app.aws import get_session
from app.config import settings

PRIMARY = "primary"
SECONDARY = "secondary"
PERMANENT = "permanent"
MONTHLY = "monthly"


def _now() -> int:
    return int(time.time())


@dataclass
class AccountMapping:
    """一条「飞书用户 ↔ Kiro 账号」映射。"""
    kiro_user_id: str
    feishu_open_id: str
    feishu_name: str = ""
    team: str = ""
    kiro_username: str = ""
    kiro_email: str = ""
    tier: str = ""
    status: str = "active"
    account_role: str = SECONDARY
    retention: str = MONTHLY
    approved_by: str = ""
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)

    def __post_init__(self):
        # retention 由 role 推导，保证一致：primary→permanent，secondary→monthly
        self.retention = PERMANENT if self.account_role == PRIMARY else MONTHLY

    def to_item(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_item(cls, item: dict) -> "AccountMapping":
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in item.items() if k in known}
        # DynamoDB 数字回来是 Decimal
        for tk in ("created_at", "updated_at"):
            if tk in data and data[tk] is not None:
                data[tk] = int(data[tk])
        return cls(**data)


class MappingStore:
    """DynamoDB 映射表封装。"""

    def __init__(self, table_name: str | None = None, gsi_name: str | None = None):
        self.table_name = table_name or settings.mapping_table_name
        self.gsi_name = gsi_name or settings.mapping_gsi_name
        self._table = get_session().resource(
            "dynamodb", region_name=settings.aws_region
        ).Table(self.table_name)

    # ---- 写 ----

    def put(self, mapping: AccountMapping) -> None:
        """新增/覆盖一条映射（新建账号开通成功后调用）。"""
        mapping.updated_at = _now()
        self._table.put_item(Item=mapping.to_item())

    def update_fields(self, kiro_user_id: str, **fields) -> None:
        """局部更新（如改 tier / status / account_role）。"""
        if not fields:
            return
        fields["updated_at"] = _now()
        # account_role 改了要同步 retention
        if "account_role" in fields:
            fields["retention"] = (
                PERMANENT if fields["account_role"] == PRIMARY else MONTHLY
            )
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in fields.items()}
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
        self._table.update_item(
            Key={"kiro_user_id": kiro_user_id},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def delete(self, kiro_user_id: str) -> None:
        """删除映射（解除关联 / 回收后清理）。不影响 IDC 账号本身。"""
        self._table.delete_item(Key={"kiro_user_id": kiro_user_id})

    # ---- 读 ----

    def get(self, kiro_user_id: str) -> AccountMapping | None:
        """正查：UserId → 映射（O(1)）。Dashboard 给用量加人名走这里。"""
        resp = self._table.get_item(Key={"kiro_user_id": kiro_user_id})
        item = resp.get("Item")
        return AccountMapping.from_item(item) if item else None

    def list_by_feishu(self, feishu_open_id: str) -> list[AccountMapping]:
        """反查：一个人的所有账号（GSI Query，支持 1:N）。"""
        resp = self._table.query(
            IndexName=self.gsi_name,
            KeyConditionExpression=Key("feishu_open_id").eq(feishu_open_id),
        )
        return [AccountMapping.from_item(i) for i in resp.get("Items", [])]

    def all_usernames(self) -> set[str]:
        """已被本系统占用的全部 kiro_username（申请校验用，避免撞名）。"""
        usernames: set[str] = set()
        kwargs: dict = {"ProjectionExpression": "kiro_username"}
        while True:
            resp = self._table.scan(**kwargs)
            usernames.update(
                i["kiro_username"] for i in resp.get("Items", []) if i.get("kiro_username")
            )
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return usernames

    def find_by_email(self, email: str) -> AccountMapping | None:
        """按 email 查映射（迁移期 email 精确匹配 / 申请邮箱去重用）。"""
        if not email:
            return None
        kwargs: dict = {
            "FilterExpression": Key("kiro_email").eq(email),
        }
        while True:
            resp = self._table.scan(**kwargs)
            for i in resp.get("Items", []):
                return AccountMapping.from_item(i)
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return None

    # ---- 业务派生 ----

    def count_active(self, feishu_open_id: str) -> int:
        """某人当前 active 账号数（配额校验用）。"""
        return sum(
            1 for m in self.list_by_feishu(feishu_open_id) if m.status == "active"
        )

    def has_primary(self, feishu_open_id: str) -> bool:
        """该用户是否已有主账号（决定新账号默认主/副）。"""
        return any(
            m.account_role == PRIMARY and m.status == "active"
            for m in self.list_by_feishu(feishu_open_id)
        )

    def list_secondary(self) -> list[AccountMapping]:
        """全部副账号（回收清单：识别 secondary）。"""
        result: list[AccountMapping] = []
        kwargs: dict = {"FilterExpression": Key("account_role").eq(SECONDARY)}
        while True:
            resp = self._table.scan(**kwargs)
            result.extend(
                AccountMapping.from_item(i)
                for i in resp.get("Items", [])
                if i.get("status") == "active"
            )
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return result