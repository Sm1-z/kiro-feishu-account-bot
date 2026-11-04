# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""API 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class ApplyIn(BaseModel):
    username: str = Field(min_length=1)
    email: EmailStr
    given_name: str = ""
    family_name: str = ""
    group: str = ""
    tier: str = Field(pattern="^(pro|pro\\+|pro max|power)$")


class UpgradeIn(BaseModel):
    kiro_user_id: str
    target_tier: str = Field(pattern="^(pro|pro\\+|pro max|power)$")


class QuotaIn(BaseModel):
    requested_quota: int = Field(gt=0)


class ReviewIn(BaseModel):
    comment: str = ""


class OverageCapIn(BaseModel):
    desired_value: float = Field(gt=0)


class DeactivateIn(BaseModel):
    """管理员停用/回收。mode 对应控制台「Deactivate Kiro plan」的两个选项。"""
    kiro_user_ids: list[str] = Field(min_length=1, max_length=200)
    mode: str = Field(default="end_of_period", pattern="^(end_of_period|immediate)$")
    recycle: bool = False  # True = 退订后同时删 IDC 用户 + 解除映射（副账号回收）


class ReactivateIn(BaseModel):
    """管理员恢复订阅（撤销停用）：对 CANCELLED 账号按原套餐重新订阅。"""
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
    tier: str = ""