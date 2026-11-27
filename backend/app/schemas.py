# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""API 请求/响应模型（积分池版）。

用量上限单位为 credits / 账期（UTC 自然月），取整数。
"""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

MAX_LIMIT = 1_000_000


class ApplyIn(BaseModel):
    """申请开通：加入积分池。usage_limit 为空 = 走组上限或 profile 默认上限。"""
    username: str = Field(min_length=1)
    email: EmailStr
    given_name: str = ""
    family_name: str = ""
    group: str = ""
    usage_limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)
    reason: str = Field(default="", max_length=500)


class LimitChangeIn(BaseModel):
    """申请调整个人用量上限（审批通过后生效）。"""
    kiro_user_id: str
    requested_limit: int = Field(ge=1, le=MAX_LIMIT)
    reason: str = Field(default="", max_length=500)


class ReviewIn(BaseModel):
    comment: str = ""


class LimitSetIn(BaseModel):
    """管理员直接设置上限。scope=USER 需 scope_id（IDC UserId）；POOL = profile 默认（每人）。

    usage_limit=None 表示删除该上限；0 表示禁止使用。
    """
    scope: str = Field(pattern="^(USER|POOL)$")
    scope_id: str = ""
    usage_limit: int | None = Field(default=None, ge=0, le=MAX_LIMIT)


class DeactivateIn(BaseModel):
    """管理员停用（移出积分池，即时生效）/ 回收（另删 IDC 用户 + 解除映射）。"""
    kiro_user_ids: list[str] = Field(min_length=1, max_length=200)
    recycle: bool = False


class ReactivateIn(BaseModel):
    """管理员恢复：重新加入积分池（预付池同时分配席位）。也用于「等待席位」账号重新分配。"""
    kiro_user_ids: list[str] = Field(min_length=1, max_length=200)


class GroupIn(BaseModel):
    group_name: str = Field(min_length=1, max_length=128)


class ImportLinkIn(BaseModel):
    kiro_user_id: str = Field(min_length=1)
    feishu_open_id: str = Field(min_length=1)
    feishu_name: str = ""


class ManualLinkIn(BaseModel):
    kiro_user_id: str
    feishu_open_id: str
    feishu_name: str = ""
    kiro_username: str = ""
    kiro_email: str = ""
