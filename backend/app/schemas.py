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


class ManualLinkIn(BaseModel):
    kiro_user_id: str
    feishu_open_id: str
    feishu_name: str = ""
    kiro_username: str = ""
    kiro_email: str = ""
    tier: str = ""