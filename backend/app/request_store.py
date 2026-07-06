# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""申请/审批记录存储（DynamoDB）。

与映射表分离的第二张表 kiro-account-requests。requests 表天然是审计日志
（谁申请、谁审批、何时、结果），见设计 04 A4。

状态机（设计 02 安全基线「审批防重」）：
    pending → approved → executed / failed
    pending → rejected
状态先于 AWS 执行落库（防多管理员重复点击触发重复开通）。

字段：
    request_id    (PK)   uuid
    user_open_id         申请人飞书 open_id
    user_name            申请人飞书姓名（快照）
    type                 apply / upgrade / quota_increase
    status               pending / approved / executed / failed / rejected
    payload              申请详情（dict）
    result               执行结果（dict：user_id+steps 或 error）
    kiro_user_id         升级类申请关联的账号锚点
    reviewer_open_id     审批人
    review_comment
    notify_message_ids   飞书审批卡片 message_id 列表（审批后更新卡片用）
    created_at / reviewed_at
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field

from boto3.dynamodb.conditions import Key

from app.aws import get_session
from app.config import settings

# 状态常量
PENDING = "pending"
APPROVED = "approved"
EXECUTED = "executed"
FAILED = "failed"
REJECTED = "rejected"

# 类型常量
APPLY = "apply"
UPGRADE = "upgrade"
QUOTA_INCREASE = "quota_increase"
OVERAGE_CAP = "overage_cap"  # 管理员调高 Overages 上限（即时执行，无审批流，纯审计记录）


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Request:
    user_open_id: str
    type: str
    payload: dict = field(default_factory=dict)
    request_id: str = field(default_factory=_new_id)
    user_name: str = ""
    status: str = PENDING
    result: dict = field(default_factory=dict)
    kiro_user_id: str = ""
    reviewer_open_id: str = ""
    review_comment: str = ""
    notify_message_ids: list = field(default_factory=list)
    created_at: int = field(default_factory=_now)
    reviewed_at: int = 0

    def to_item(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}

    @classmethod
    def from_item(cls, item: dict) -> "Request":
        known = set(cls.__dataclass_fields__)
        data = {k: v for k, v in item.items() if k in known}
        for tk in ("created_at", "reviewed_at"):
            if tk in data and data[tk] is not None:
                data[tk] = int(data[tk])
        return cls(**data)


class RequestStore:
    def __init__(self, table_name: str | None = None):
        self.table_name = table_name or f"{settings.mapping_table_name}-requests"
        self._table = get_session().resource(
            "dynamodb", region_name=settings.aws_region
        ).Table(self.table_name)

    def create(self, req: Request) -> Request:
        self._table.put_item(Item=req.to_item())
        return req

    def get(self, request_id: str) -> Request | None:
        item = self._table.get_item(Key={"request_id": request_id}).get("Item")
        return Request.from_item(item) if item else None

    def update_fields(self, request_id: str, **fields) -> None:
        if not fields:
            return
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in fields.items()}
        self._table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def transition(self, request_id: str, from_status: str, to_status: str,
                   **extra) -> bool:
        """条件更新状态：仅当当前状态 == from_status 才置为 to_status。

        这是审批防重的关键：多管理员/飞书重试同时点"通过"，只有第一个
        （pending→approved）成功，其余因条件不满足返回 False，不会重复执行。
        """
        names = {"#status": "status"}
        values = {":to": to_status, ":from": from_status}
        set_parts = ["#status = :to"]
        for k, v in extra.items():
            names[f"#{k}"] = k
            values[f":{k}"] = v
            set_parts.append(f"#{k} = :{k}")
        try:
            self._table.update_item(
                Key={"request_id": request_id},
                UpdateExpression="SET " + ", ".join(set_parts),
                ConditionExpression="#status = :from",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def list_by_user(self, user_open_id: str) -> list[Request]:
        """某人的申请记录（需 GSI user_open_id-index；无则降级 scan）。"""
        try:
            resp = self._table.query(
                IndexName="user_open_id-index",
                KeyConditionExpression=Key("user_open_id").eq(user_open_id),
            )
            items = resp.get("Items", [])
        except Exception:
            items = [i for i in self._scan_all() if i.get("user_open_id") == user_open_id]
        reqs = [Request.from_item(i) for i in items]
        return sorted(reqs, key=lambda r: r.created_at, reverse=True)

    def list_by_status(self, status: str | None = None) -> list[Request]:
        """全部申请，可按状态过滤（管理员审批列表）。"""
        reqs = [Request.from_item(i) for i in self._scan_all()]
        if status:
            reqs = [r for r in reqs if r.status == status]
        return sorted(reqs, key=lambda r: r.created_at, reverse=True)

    def _scan_all(self) -> list[dict]:
        items, kwargs = [], {}
        while True:
            resp = self._table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return items