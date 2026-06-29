# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""飞书 OpenAPI 客户端：OAuth 登录 + 消息卡片收发。

仅封装 HTTP API（token / OAuth / 发卡片 / 更新卡片）。WebSocket 长连接见 feishu_ws.py。
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE = "https://open.feishu.cn/open-apis"

# tenant_access_token 简单缓存（飞书有效期 ~2h）
_token_cache: dict = {"token": "", "exp": 0}


def get_tenant_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["exp"] - 60:
        return _token_cache["token"]
    resp = httpx.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["exp"] = time.time() + data.get("expire", 7200)
    return _token_cache["token"]


# ---------------------------------------------------------------------------
# OAuth 登录
# ---------------------------------------------------------------------------

def oauth_authorize_url(state: str) -> str:
    """生成飞书授权跳转 URL。"""
    return (
        f"{BASE}/authen/v1/authorize"
        f"?app_id={settings.feishu_app_id}"
        f"&redirect_uri={settings.feishu_redirect_uri}"
        f"&state={state}"
    )


def oauth_exchange_user(code: str) -> dict:
    """用 OAuth code 换取用户信息。返回 {open_id, union_id, name, avatar_url, email}。"""
    token = get_tenant_token()
    resp = httpx.post(
        f"{BASE}/authen/v1/access_token",
        headers={"Authorization": f"Bearer {token}"},
        json={"grant_type": "authorization_code", "code": code},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"OAuth 换取用户失败: {data}")
    d = data["data"]
    return {
        "open_id": d.get("open_id", ""),
        "union_id": d.get("union_id", ""),
        "name": d.get("name", ""),
        "avatar_url": d.get("avatar_url", ""),
        "email": d.get("email", "") or d.get("enterprise_email", ""),
    }


# ---------------------------------------------------------------------------
# 消息卡片
# ---------------------------------------------------------------------------

def send_card(open_id: str, card: dict) -> str:
    """给指定用户发交互卡片。返回 message_id（用于后续更新卡片）。"""
    token = get_tenant_token()
    resp = httpx.post(
        f"{BASE}/im/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={"receive_id_type": "open_id"},
        json={"receive_id": open_id, "msg_type": "interactive",
              "content": _json(card)},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"发送卡片失败: {data}")
    return data["data"]["message_id"]


def update_card(message_id: str, card: dict) -> None:
    """更新已发出的卡片（审批后把按钮换成最终状态）。"""
    token = get_tenant_token()
    resp = httpx.patch(
        f"{BASE}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": _json(card)},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        logger.warning("更新卡片失败 message_id=%s: %s", message_id, data)


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)