# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kiro 服务接口 SigV4 直连（积分池版）。

Kiro 积分池（控制台名 Credit sharing）的管理接口目前没有随 boto3 发布，这里按
JSON-1.0 协议手动 SigV4 签名调用。三类端点：

| 端点 | X-Amz-Target 前缀 | 签名服务名 | 用途 |
|------|------------------|-----------|------|
| management.{region}.kiro.dev | KiroControlPlaneQDeveloperService. | q | 池成员 / 席位 / 用量上限 / 购买记录（写操作只能走这里） |
| service.user-subscriptions.{region}.amazonaws.com | AWSZornControlPlaneService. | user-subscriptions | 只读：席位状态、池余额、生效上限 |
| identitystore.{region}.amazonaws.com | SWBUPService. | userpool | 发送密码设置邮件 |

错误统一抛 KiroApiError（kind：conflict / not_found / throttled / access_denied /
validation / other），限流（429）指数退避重试。接口为非公开接口，AWS 侧变更可能需要适配，
所有调用都收敛在本模块。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import botocore.auth
import botocore.awsrequest

from app.aws import get_frozen_credentials
from app.config import settings

CPS_TARGET = "KiroControlPlaneQDeveloperService."
ZORN_TARGET = "AWSZornControlPlaneService."

_KIND_BY_TYPE = {
    "ConflictException": "conflict",
    "ResourceNotFoundException": "not_found",
    "ThrottlingException": "throttled",
    "TooManyRequestsException": "throttled",
    "AccessDeniedException": "access_denied",
    "ValidationException": "validation",
}


class KiroApiError(RuntimeError):
    """Kiro 接口调用失败。kind 用于调用方做幂等 / 提示分流。"""

    def __init__(self, op: str, status: int, kind: str, message: str):
        super().__init__(f"{op} failed: HTTP {status} {kind}: {message}")
        self.op = op
        self.status = status
        self.kind = kind
        self.message = message


def _classify(status: int, body: str) -> tuple[str, str]:
    etype, msg = "", body
    try:
        data = json.loads(body)
        etype = str(data.get("__type") or data.get("code") or "").split("#")[-1]
        msg = data.get("message") or data.get("Message") or body
    except (ValueError, AttributeError):
        etype = next((t for t in _KIND_BY_TYPE if t in body), "")
    kind = _KIND_BY_TYPE.get(etype) or ("throttled" if status == 429 else "other")
    return kind, str(msg)[:500]


def signed_post(url: str, target: str, service: str, payload: dict, *,
                credentials=None, region: str | None = None, timeout: int = 30) -> dict:
    """对 JSON-1.0 接口做 SigV4 签名并 POST，返回解析后的响应体。"""
    region = region or settings.aws_region
    credentials = credentials or get_frozen_credentials()
    body = json.dumps(payload)
    req = botocore.awsrequest.AWSRequest(method="POST", url=url, data=body, headers={
        "Content-Type": "application/x-amz-json-1.0", "X-Amz-Target": target})
    botocore.auth.SigV4Auth(credentials, service, region).add_auth(req)
    http_req = urllib.request.Request(url, data=body.encode(), headers=dict(req.headers),
                                      method="POST")
    op = target.split(".")[-1]
    try:
        raw = urllib.request.urlopen(http_req, timeout=timeout).read()
    except urllib.error.HTTPError as exc:
        kind, msg = _classify(exc.code, exc.read().decode(errors="replace"))
        raise KiroApiError(op, exc.code, kind, msg) from exc
    return json.loads(raw.decode() or "{}")


def _with_retry(fn, max_retries: int):
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except KiroApiError as exc:
            if exc.kind == "throttled" and attempt < max_retries:
                time.sleep(min(2 * 2 ** attempt, 30))
                continue
            raise
    raise RuntimeError("unreachable")


def cps_endpoint(region: str | None = None) -> str:
    return f"https://management.{region or settings.aws_region}.kiro.dev/"


def zorn_endpoint(region: str | None = None) -> str:
    return f"https://service.user-subscriptions.{region or settings.aws_region}.amazonaws.com/"


def cps(op: str, payload: dict | None = None, *, credentials=None, max_retries: int = 3) -> dict:
    """Kiro 控制面（池管理）。"""
    return _with_retry(lambda: signed_post(
        cps_endpoint(), CPS_TARGET + op, "q", payload or {}, credentials=credentials), max_retries)


def zorn(op: str, payload: dict, *, credentials=None, max_retries: int = 3) -> dict:
    """订阅服务（只读：席位、余额、生效上限）。"""
    return _with_retry(lambda: signed_post(
        zorn_endpoint(), ZORN_TARGET + op, "user-subscriptions", payload,
        credentials=credentials), max_retries)


def paginate(call, payload: dict, items_key: str, page_size: int = 100) -> list:
    """按 nextToken 翻页，返回全部条目。call = lambda payload: dict。"""
    out, token = [], None
    while True:
        p = dict(payload, maxResults=page_size)
        if token:
            p["nextToken"] = token
        resp = call(p)
        out.extend(resp.get(items_key) or [])
        token = resp.get("nextToken")
        if not token:
            return out


def send_password_email(user_id: str, *, credentials=None) -> None:
    """让 IAM Identity Center 给用户发送密码设置邮件。"""
    region = settings.aws_region
    signed_post(f"https://identitystore.{region}.amazonaws.com/", "SWBUPService.UpdatePassword",
                "userpool", {"UserId": user_id, "PasswordMode": "EMAIL"},
                credentials=credentials)
