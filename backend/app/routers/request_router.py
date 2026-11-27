# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""申请路由（普通用户）：开通（加入积分池，每人一个账号）/ 调整用量上限 + 我的申请。

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
    APPLY, APPROVED, LIMIT_CHANGE, PENDING, Request, RequestStore,
)
from app.schemas import ApplyIn, LimitChangeIn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/requests", tags=["requests"])


def _username_exists_in_idc(username: str) -> bool:
    """实查 IDC 用户名是否已存在。查询失败保守放行（审批执行时仍有 Conflict 兜底）。"""
    from app.aws import get_identity_store_id, get_session
    try:
        session = get_session()
        id_store = get_identity_store_id(session)
        idc = session.client("identitystore", region_name=settings.aws_region)
        try:
            idc.get_user_id(
                IdentityStoreId=id_store,
                AlternateIdentifier={"UniqueAttribute": {
                    "AttributePath": "userName", "AttributeValue": username}},
            )
            return True
        except idc.exceptions.ResourceNotFoundException:
            return False
    except Exception:
        logger.exception("IDC 用户名占用检查失败，放行由执行步兜底")
        return False


def _pool_hint(req_type: str) -> str:
    """审批卡片上的积分池现状一句话（剩余 credits / 席位），拿不到就不显示。"""
    try:
        from app import pool
        ctx = pool.get_context()
        ov = pool.overview()
        seats = pool.seat_status()
        parts = [f"积分池剩余 {ov['total_left']:.0f} / {ov['total_quantity']:.0f} credits",
                 f"已占座 {seats.get('seated') or 0} 人"]
        if req_type == APPLY:
            if ctx.prepaid:
                avail = seats.get("available")
                parts.append(f"预付：空余席位 {avail} 个" if avail
                             else "预付：没有空余席位，通过后需先在控制台买席位")
            else:
                parts.append("后付：通过后自动占座，每人 $20/月（首月按天折算）")
        return " · ".join(parts)
    except Exception:  # noqa: BLE001
        logger.warning("pool hint unavailable", exc_info=True)
        return ""


def _push_approval_cards(req: Request) -> list[str]:
    """给所有管理员推审批卡片，返回 message_id 列表。飞书失败不阻断申请落库。"""
    mids: list[str] = []
    card = cards.build_approval_card(req.request_id, req.type, req.user_name, req.payload,
                                     pool_hint=_pool_hint(req.type))
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

    # 每人一个账号：积分池里 credits 全员共享，多开账号只会多占一个席位（$20/月）
    existing = store.list_by_feishu(user.open_id)
    if existing:
        raise HTTPException(400, f"你已有 Kiro 账号（{existing[0].kiro_username}），每人只能有一个账号；"
                                 "账号被停用的话请联系管理员恢复")
    if any(r.type == APPLY and r.status in (PENDING, APPROVED) for r in reqs.list_by_user(user.open_id)):
        raise HTTPException(400, "你已有一条开通申请在处理中，请等审批结果")

    # 用户名占用：先查映射表（平台开的账号），再实查 IDC——IDC 里可能有
    # 非平台创建的存量用户（如 SRE 手工建的），只查映射表拦不住，
    # 会到审批执行时才撞 Conflict，用户只看到 failed 不知道原因。
    if body.username in store.all_usernames():
        raise HTTPException(400, f"用户名 '{body.username}' 已被占用")
    if _username_exists_in_idc(body.username):
        raise HTTPException(400, f"用户名 '{body.username}' 在 IDC 中已存在，请更换用户名")
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


@router.post("/limit-change")
def limit_change(body: LimitChangeIn, user: CurrentUser = Depends(current_user)):
    """申请调整个人用量上限（credits / 月），审批通过后即时生效。"""
    store = MappingStore()
    reqs = RequestStore()
    acct = store.get(body.kiro_user_id)
    if not acct or acct.feishu_open_id != user.open_id:
        raise HTTPException(404, "账号不存在或不属于你")
    if acct.status != "active":
        raise HTTPException(400, "账号已停用，不能调整上限")
    current = None
    try:
        from app import pool
        eff = pool.effective_limit(body.kiro_user_id)
        current = int(eff["limit"]) if eff else None
    except Exception:  # noqa: BLE001 - 当前值只用于卡片展示
        logger.warning("limit_change: effective limit unavailable")

    req = reqs.create(Request(
        user_open_id=user.open_id, user_name=user.name, type=LIMIT_CHANGE,
        kiro_user_id=body.kiro_user_id,
        payload={"username": acct.kiro_username, "requested_limit": body.requested_limit,
                 "current_limit": current, "reason": body.reason},
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