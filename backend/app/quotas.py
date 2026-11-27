# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kiro Service Quotas 只读查询（积分池相关的两项）。

| QuotaCode | 名称 | 对积分池的意义 |
|-----------|------|----------------|
| L-295A3FFB | Maximum allowed users to onboard per Kiro profile | 可接入的用户数上限；后付池到上限后新成员显示「等待席位」 |
| L-05AF9163 | Maximum allowed credits to purchase in month | 每月最多可购买的追加 credits |

只读、降级安全：查询失败该项为 None，不阻断页面；值变化频率低，缓存 10 分钟。
提额在 Service Quotas 控制台发起（管理页给出链接）。
"""
from __future__ import annotations

import logging
import threading
import time

from botocore.exceptions import ClientError

from app.aws import get_session
from app.config import settings

logger = logging.getLogger(__name__)

QUOTA_SERVICE_CODE = "kiro"
QUOTAS = {
    "onboarding_cap": "L-295A3FFB",
    "monthly_purchase_cap": "L-05AF9163",
}

_CACHE_TTL = 600
_cache: dict = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def console_url(quota_code: str) -> str:
    # Kiro 的 quota 只在 us-east-1 的 Service Quotas 控制台可见
    return (f"https://us-east-1.console.aws.amazon.com/servicequotas/home/services/"
            f"{QUOTA_SERVICE_CODE}/quotas/{quota_code}")


def _fetch_one(client, code: str) -> dict | None:
    try:
        quota = client.get_service_quota(ServiceCode=QUOTA_SERVICE_CODE, QuotaCode=code)["Quota"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchResourceException":
            raise
        quota = client.get_aws_default_service_quota(
            ServiceCode=QUOTA_SERVICE_CODE, QuotaCode=code)["Quota"]
    return {"code": code, "name": quota.get("QuotaName", ""), "value": quota.get("Value"),
            "adjustable": quota.get("Adjustable", False), "console_url": console_url(code)}


def get_kiro_quotas(force: bool = False) -> dict:
    """{key: {code, name, value, adjustable, console_url} 或 None}，带 TTL 缓存。"""
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
            return _cache["data"]
    try:
        client = get_session().client("service-quotas", region_name=settings.aws_region)
    except Exception:  # noqa: BLE001
        logger.exception("service-quotas client 不可用")
        return {k: None for k in QUOTAS}
    data = {}
    for key, code in QUOTAS.items():
        try:
            data[key] = _fetch_one(client, code)
        except Exception:  # noqa: BLE001
            logger.warning("查询 Kiro quota %s 失败，降级为 None", code)
            data[key] = None
    with _lock:
        _cache["ts"], _cache["data"] = time.time(), data
    return data
