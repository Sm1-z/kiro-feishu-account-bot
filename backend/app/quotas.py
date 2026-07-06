# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kiro Service Quotas（Overages 超额上限）只读查询。

Kiro 企业版支持通过 AWS Service Quotas 控制 profile 内每个订阅的超额上限
（quota: "Maximum allowed overage per Kiro profile"，单位 USD）。
本模块查询该 quota 当前值，供管理页展示成本敞口。

注意：该上限是 profile 级单一值（非按用户），且 RequestServiceQuotaIncrease
只接受大于当前值的请求（实测 IllegalArgumentException），调低需走 AWS Support
case——所以本平台只做只读展示，不做调整写入口。

设计要点（与 app.usage 一致）：
- **只读、降级安全**：查询失败 / 无权限 → 返回 None，管理页显示 —，不阻断页面。
- **TTL 缓存**：quota 值变化频率极低，缓存 10 分钟，避免每次开页面都打 API。
- 凭证走 app.aws 默认链（Role，无 AK/SK）。
"""
from __future__ import annotations

import logging
import threading
import time

from botocore.exceptions import ClientError

from app.aws import get_session
from app.config import settings

logger = logging.getLogger(__name__)

# Kiro 在 Service Quotas 的服务码与 overage quota 码（实测验证，见 docs）
QUOTA_SERVICE_CODE = "kiro"
OVERAGE_QUOTA_CODE = "L-75434B0B"

_CACHE_TTL = 600  # 秒
_cache: dict = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def _fetch_overage_cap() -> dict:
    """查 quota 当前值。账号无 applied 值时回退查服务默认值。"""
    client = get_session().client("service-quotas", region_name=settings.aws_region)
    try:
        quota = client.get_service_quota(
            ServiceCode=QUOTA_SERVICE_CODE, QuotaCode=OVERAGE_QUOTA_CODE
        )["Quota"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchResourceException":
            raise
        quota = client.get_aws_default_service_quota(
            ServiceCode=QUOTA_SERVICE_CODE, QuotaCode=OVERAGE_QUOTA_CODE
        )["Quota"]
    return {
        "value": quota.get("Value"),
        "quota_name": quota.get("QuotaName", ""),
        "adjustable": quota.get("Adjustable", False),
        "region": settings.aws_region,
        "console_url": (
            f"https://{settings.aws_region}.console.aws.amazon.com/servicequotas/home/"
            f"services/{QUOTA_SERVICE_CODE}/quotas/{OVERAGE_QUOTA_CODE}"
        ),
    }


def get_overage_cap(force: bool = False) -> dict | None:
    """Overage 上限信息（USD/订阅），带 TTL 缓存。失败返回 None（降级）。"""
    now = time.time()
    with _lock:
        if not force and (now - _cache["ts"]) < _CACHE_TTL and _cache["data"] is not None:
            return _cache["data"]
    try:
        data = _fetch_overage_cap()
        with _lock:
            _cache["ts"], _cache["data"] = time.time(), data
        return data
    except Exception:
        logger.exception("查询 Kiro overage quota 失败，降级为 None")
        return _cache["data"]
