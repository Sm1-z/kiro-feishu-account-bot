# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""认证路由：飞书 OAuth 登录 + 当前用户信息。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from app import feishu
from app.auth import CurrentUser, current_user, issue_token
from app.config import settings
from app.resolver import Resolver, suggest_new_username

router = APIRouter(prefix="/api/auth", tags=["auth"])

# state 防 CSRF（简单内存集合；多实例可换 Redis/签名 state）
_states: set[str] = set()


@router.get("/feishu/login")
def feishu_login():
    state = secrets.token_urlsafe(16)
    _states.add(state)
    return RedirectResponse(feishu.oauth_authorize_url(state))


@router.get("/feishu/callback")
def feishu_callback(code: str, state: str = ""):
    # state 校验（宽松：缺失也放行以便本地调试，可收紧）
    _states.discard(state)
    info = feishu.oauth_exchange_user(code)
    if not info.get("open_id"):
        raise HTTPException(400, "OAuth 失败：未获取 open_id")
    token = issue_token(info["open_id"], info["name"])
    # 重定向回前端并带 token
    return RedirectResponse(f"{settings.frontend_url}/auth/callback?token={token}")


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)):
    from app import pool, usage
    from app.provisioner import live_status_map

    resolver = Resolver()
    accounts = resolver.accounts_of(user.open_id)
    taken = resolver.store.all_usernames()

    # JOIN 积分池实况：映射表的 status 是平台操作时的快照，管理员在控制台直接移出 / 加入
    # 不会回写，以实况为准展示（拉取失败降级用快照，live_synced=False）。
    ids = [a.kiro_user_id for a in accounts]
    live, limits, month, live_synced = {}, {}, {}, True
    if ids:  # 没有账号就不查 AWS
        try:
            live = live_status_map()
        except Exception:
            live_synced = False
        try:
            limits = pool.effective_limits(ids)
        except Exception:
            limits = {}
        month = usage.get_current_month_usage_by_user()
    out = []
    for a in accounts:
        d = dict(a.__dict__)
        uid = a.kiro_user_id
        lv = live.get(uid)
        d["live_synced"] = live_synced
        d["pool_status"] = lv["status"] if lv else None  # None = 不在积分池
        if uid in limits:
            eff = limits[uid]
            d["limit"] = eff["limit"] if eff else None
            d["limit_scope"] = eff["scope"] if eff else "NONE"
        else:
            d["limit"], d["limit_scope"] = None, None  # 查询失败（前端显示 —）
        mu = month.get(uid)
        d["month_credits"] = mu["credits"] if mu else (0.0 if month else None)
        out.append(d)

    return {
        "open_id": user.open_id,
        "name": user.name,
        "is_admin": user.is_admin,
        "quota": settings.default_account_quota,
        "accounts": out,
        "suggested_username": suggest_new_username(user.name, taken),
    }