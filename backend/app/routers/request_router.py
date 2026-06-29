# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""申请路由（普通用户）：开通 / 升级 / 配额 + 我的申请。

提交时做前置校验（设计 PRD §3.4）：配额、用户名占用、邮箱唯一。
提交后推飞书审批卡片给所有管理员，记录 message_id 供审批后更新卡片。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app import cards, feishu
from app.auth import CurrentUser, current_user
from app.config import settings
from app.mapping_store import MappingStore
from app.request_store import (
    APPLY, QUOTA_INCREASE, UPGRADE, Request, RequestStore,
)
from app.schemas import ApplyIn, QuotaIn, UpgradeIn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/requests", tags=["requests"])


def _push_approval_cards(req: Request) -> list[str]:
    """给所有管理员推审批卡片，返回 message_id 列表。飞书失败不阻断申请落库。"""
    mids: list[str] = []
    card = cards.build_approval_card(req.request_id, req.type, req.user_name, req.payload)
    for admin in settings.admin_id_set():
        try:
            mids.append(feishu.send_card(admin, card))
        except Exception:
            logger.exception("推送审批卡片失败 admin=%s", admin)
    return mids


@router.post("/apply")
def apply(body: ApplyIn, user: CurrentUser = Depends(current_user)):
    store = MappingStore()
    reqs = RequestStore()

    # 账号数量不设上限（不限制每人可申请的 Kiro 账号数）

    # 用户名占用
    if body.username in store.all_usernames():
        raise HTTPException(400, f"用户名 '{body.username}' 已被占用")
    # 邮箱唯一（IDC 约束本地拦截）
    if store.find_by_email(body.email):
        raise HTTPException(400, f"邮箱 '{body.email}' 已被使用")

    req = reqs.create(Request(
        user_open_id=user.open_id, user_name=user.name, type=APPLY,
        payload=body.model_dump(),
    ))
    mids = _push_approval_cards(req)
    if mids:
        reqs.update_fields(req.request_id, notify_message_ids=mids)
    return {"request_id": req.request_id, "status": req.status}


@router.post("/upgrade")
def upgrade(body: UpgradeIn, user: CurrentUser = Depends(current_user)):
    store = MappingStore()
    reqs = RequestStore()
    acct = store.get(body.kiro_user_id)
    if not acct or acct.feishu_open_id != user.open_id:
        raise HTTPException(404, "账号不存在或不属于你")
    if acct.status != "active":
        raise HTTPException(400, "仅 active 账号可升级")

    req = reqs.create(Request(
        user_open_id=user.open_id, user_name=user.name, type=UPGRADE,
        kiro_user_id=body.kiro_user_id,
        payload={"username": acct.kiro_username, "target_tier": body.target_tier},
    ))
    mids = _push_approval_cards(req)
    if mids:
        reqs.update_fields(req.request_id, notify_message_ids=mids)
    return {"request_id": req.request_id, "status": req.status}


@router.post("/quota-increase")
def quota_increase(body: QuotaIn, user: CurrentUser = Depends(current_user)):
    if body.requested_quota <= settings.default_account_quota:
        raise HTTPException(400, "请求配额需大于当前配额")
    reqs = RequestStore()
    req = reqs.create(Request(
        user_open_id=user.open_id, user_name=user.name, type=QUOTA_INCREASE,
        payload={"requested_quota": body.requested_quota},
    ))
    mids = _push_approval_cards(req)
    if mids:
        reqs.update_fields(req.request_id, notify_message_ids=mids)
    return {"request_id": req.request_id, "status": req.status}


@router.get("/mine")
def mine(user: CurrentUser = Depends(current_user)):
    return [r.__dict__ for r in RequestStore().list_by_user(user.open_id)]


@router.get("/groups")
def groups(_: CurrentUser = Depends(current_user)):
    """可申请的分组：从 IDC 动态拉取 list_groups。

    避免前端写死组名（换客户/环境无需改代码重新 build）。失败时降级为
    配置里的默认组，保证申请表单仍可用。
    """
    from app.aws import get_identity_store_id, get_session
    try:
        session = get_session()
        id_store = get_identity_store_id(session)
        idc = session.client("identitystore", region_name=settings.aws_region)
        names, kwargs = [], {"IdentityStoreId": id_store}
        while True:
            resp = idc.list_groups(**kwargs)
            names.extend(g["DisplayName"] for g in resp.get("Groups", []))
            token = resp.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token
        return sorted(names)
    except Exception:
        logger.exception("拉取 IDC 分组失败，降级为默认组")
        return [settings.kiro_group_name] if settings.kiro_group_name else []