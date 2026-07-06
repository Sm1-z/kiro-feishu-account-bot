# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kiro Service Quotas（Overages 超额上限）只读查询。

Kiro 企业版支持通过 AWS Service Quotas 控制 profile 内每个订阅的超额上限
（quota: "Maximum allowed overage per Kiro profile"，单位 USD）。
本模块查询该 quota 当前值，供管理页展示成本敞口。

注意：该上限是 profile 级单一值（非按用户），且 RequestServiceQuotaIncrease
只接受大于当前值的请求（实测 IllegalArgumentException），调低需走 AWS Support
case——调高入口做在应用内（含前置校验 + 审计落 requests 表），调低只能提示走 Support。

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


# ---------------------------------------------------------------------------
# 调高（increase-only）
# ---------------------------------------------------------------------------

# 单次最多调高为当前值的 N 倍，防误触把成本敞口一次拉爆
MAX_INCREASE_FACTOR = 2


def get_pending_cap_request() -> dict | None:
    """审批中的 quota 调整请求（PENDING/CASE_OPENED）。无则 None。不缓存——提交后要立刻看到。"""
    client = get_session().client("service-quotas", region_name=settings.aws_region)
    resp = client.list_requested_service_quota_change_history_by_quota(
        ServiceCode=QUOTA_SERVICE_CODE, QuotaCode=OVERAGE_QUOTA_CODE
    )
    for r in resp.get("RequestedQuotas", []):
        if r.get("Status") in ("PENDING", "CASE_OPENED"):
            return {
                "desired_value": r.get("DesiredValue"),
                "status": r.get("Status"),
                "requested_at": r.get("Created").isoformat() if r.get("Created") else "",
            }
    return None


def request_cap_increase(desired_value: float) -> dict:
    """发起调高。校验失败/API 拒绝抛 ValueError（含用户可读信息）。

    幂等考虑：Service Quotas 对同一 quota 已有 pending 请求时会拒绝新请求，
    这里先查 pending 提前给出友好提示。
    """
    current = get_overage_cap(force=True)
    if current is None:
        raise ValueError("无法获取当前上限值，请稍后重试")
    cur_val = current["value"]
    if desired_value <= cur_val:
        raise ValueError(
            f"新上限必须大于当前值 ${cur_val:g}。上限只可调高；调低需联系 AWS Support。"
        )
    if desired_value > cur_val * MAX_INCREASE_FACTOR:
        raise ValueError(
            f"单次最多调高至当前值的 {MAX_INCREASE_FACTOR} 倍（${cur_val * MAX_INCREASE_FACTOR:g}）。"
            f"如需更高请分次申请。"
        )
    pending = get_pending_cap_request()
    if pending:
        raise ValueError(
            f"已有一个审批中的调整请求（目标 ${pending['desired_value']:g}），请等其完成后再提交。"
        )
    client = get_session().client("service-quotas", region_name=settings.aws_region)
    try:
        resp = client.request_service_quota_increase(
            ServiceCode=QUOTA_SERVICE_CODE, QuotaCode=OVERAGE_QUOTA_CODE,
            DesiredValue=float(desired_value),
        )
    except ClientError as exc:
        raise ValueError(f"申请失败: {exc.response['Error']['Message']}") from exc
    rq = resp["RequestedQuota"]
    # 让下次读取拿到新值（小额调整通常秒批）
    with _lock:
        _cache["ts"] = 0.0
    return {
        "desired_value": rq.get("DesiredValue"),
        "status": rq.get("Status", ""),
        "request_id": rq.get("Id", ""),
    }
