# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""认证路由：飞书 OAuth 登录 + 当前用户信息。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import feishu
from app.auth import CurrentUser, current_user, issue_token
from app.config import settings
from app.resolver import Resolver, suggest_new_username

router = APIRouter(prefix="/api/auth", tags=["auth"])

# OAuth state 防登录 CSRF：登录时把随机 state 写进 HttpOnly cookie，回调时要求 URL 里的 state
# 与 cookie 一致。state 绑定发起登录的浏览器，且不依赖进程内存（多实例部署也成立）。
_STATE_COOKIE = "kiro_oauth_state"
_STATE_TTL = 600


def _secure_cookie() -> bool:
    return settings.feishu_redirect_uri.startswith("https://")


@router.get("/feishu/login")
def feishu_login():
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(feishu.oauth_authorize_url(state))
    resp.set_cookie(_STATE_COOKIE, state, max_age=_STATE_TTL, httponly=True, samesite="lax",
                    secure=_secure_cookie(), path="/api/auth/feishu")
    return resp


@router.get("/feishu/callback")
def feishu_callback(request: Request, code: str, state: str = ""):
    expected = request.cookies.get(_STATE_COOKIE, "")
    if not state or not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(400, "登录状态校验失败，请从登录页重新发起飞书登录")
    info = feishu.oauth_exchange_user(code)
    if not info.get("open_id"):
        raise HTTPException(400, "OAuth 失败：未获取 open_id")
    token = issue_token(info["open_id"], info["name"])
    # token 放在 URL fragment（#）里：不会发给服务器，也不会出现在访问日志和 Referer 中
    resp = RedirectResponse(f"{settings.frontend_url}/auth/callback#token={token}")
    resp.delete_cookie(_STATE_COOKIE, path="/api/auth/feishu")
    return resp


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