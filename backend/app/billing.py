# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""按用户的 Kiro 费用 / 额度 / 超额（Kiro Enterprise，订阅计费服务直读）。

补 Athena 用量报表没有的「钱」的维度，供管理页账号总览每行展示、汇总成本月预估账单，
以及出账前对账 / 计费争议时按月回溯。只覆盖 IAM Identity Center 企业订阅。

数据源（与 provisioner 同一套 SigV4 手签直连，service=user-subscriptions，appCode=Kiro）：
- GetCurrentPlanDetails(userId)   当前套餐 / 生效日期 / 本周期已产生费用 / 账户状态 / 待生效变更
- ListUsagesAndLimits(userId)     CREDIT 月额度 / 当前用量 / 超额费用 / 超额单价；
                                   带 timeRange 可查历史月（endTime 必须 = 月末 23:59:59 UTC）

设计要点：
- **只读、降级安全**：任一用户查询失败 → 该用户字段为 None，不阻断账号列表；两接口相互独立。
- **TTL 缓存 60s**：/accounts 每次开页都打；按用户逐个调用，用线程池并发。
- 口径提示：按用户的本月用量以 User Activity Report 为准（与控制台 Dashboard 一致）；
  本模块返回的 credit_used / overage_charges 仅作参考，界面以报表月聚合展示。
"""
from __future__ import annotations

import calendar
import datetime as _dt
import json
import logging
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from app.aws import get_frozen_credentials
from app.config import settings
from app.provisioner import _signed_post

logger = logging.getLogger(__name__)

APP_CODE = "Kiro"
DEFAULT_OVERAGE_RATE = 0.04  # USD/credit，计费服务未返回单价时的兜底（Kiro 公开定价）
_TARGET = "AWSZornControlPlaneService."
_CACHE_TTL = 60  # 秒
_cache: dict = {"ts": 0.0, "data": {}}
_lock = threading.Lock()
_MAX_WORKERS = 8


def _endpoint(region: str) -> str:
    return f"https://service.user-subscriptions.{region}.amazonaws.com/"


def _call(target: str, payload: dict, credentials, region: str) -> dict:
    try:
        raw = _signed_post(_endpoint(region), _TARGET + target, "user-subscriptions",
                           region, payload, credentials)
        return json.loads(raw.decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"{target} failed: HTTP {exc.code}: {body}") from exc


# ---------------------------------------------------------------------------
# 单用户
# ---------------------------------------------------------------------------

def _plan_details(user_id: str, credentials, region: str) -> dict:
    """GetCurrentPlanDetails → 扁平化。只能传 userId（与 accountId 互斥）。"""
    resp = _call("GetCurrentPlanDetails", {"appCode": APP_CODE, "userId": user_id},
                 credentials, region)
    ud = resp.get("userDetails") or {}
    plan = ud.get("currentPlan") or {}
    charges = (ud.get("billingDetails") or {}).get("currentCharges") or {}
    pending = ud.get("pendingPlan") or {}
    return {
        "plan_code": plan.get("planCode"),
        "plan_start": _ts(plan.get("startDate")),
        "current_charges": _num(charges.get("amount")),
        "currency": charges.get("currency") or "USD",
        "account_status": ud.get("accountStatus"),
        "pending_plan": pending.get("planCode"),
        "pending_scheduled": _ts(pending.get("scheduledDate")),
    }


def _usage_and_limits(user_id: str, credentials, region: str,
                      time_range: dict | None = None) -> dict:
    """ListUsagesAndLimits → 取 CREDIT 一条扁平化。"""
    payload = {"appCode": APP_CODE, "userId": user_id, "subscriptionRegion": region}
    if time_range:
        payload["timeRange"] = time_range
    resp = _call("ListUsagesAndLimits", payload, credentials, region)
    entries = resp.get("usagesAndLimits") or []
    credit = next((e for e in entries if e.get("appFeatureCode") == "CREDIT"), None)
    if credit is None:
        credit = entries[0] if entries else {}
    um = credit.get("usageMetrics") or {}
    cur, lim = um.get("currentUsage") or {}, um.get("usageLimit") or {}
    over = (credit.get("overageCharges") or {}).get("currentCharges") or {}
    rate = (credit.get("pricingInfo") or {}).get("overageRate") or {}
    return {
        "credit_used": _num(cur.get("value")),
        "credit_limit": _num(lim.get("value")),
        "credit_unit": cur.get("unit") or lim.get("unit"),
        "overage_charges": _num(over.get("amount")),
        "overage_rate": _num(rate.get("amount")),
        "has_usage_entry": bool(entries),
    }


def get_user_billing(user_id: str, credentials=None, region: str | None = None) -> dict:
    """单用户费用 + 用量。两接口独立降级：失败的那半边字段为 None 并记 error。"""
    region = region or settings.aws_region
    credentials = credentials or get_frozen_credentials()
    out: dict = {"billing_synced": True, "billing_error": ""}
    errors = []
    try:
        out.update(_plan_details(user_id, credentials, region))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"plan: {exc}")
        out.update({"plan_code": None, "plan_start": None, "current_charges": None,
                    "currency": "USD", "account_status": None, "pending_plan": None,
                    "pending_scheduled": None})
    try:
        out.update(_usage_and_limits(user_id, credentials, region))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"usage: {exc}")
        out.update({"credit_used": None, "credit_limit": None, "credit_unit": None,
                    "overage_charges": None, "overage_rate": None, "has_usage_entry": False})
    if errors:
        out["billing_synced"] = False
        out["billing_error"] = "; ".join(errors)[:500]
    return out


# ---------------------------------------------------------------------------
# 批量（/accounts 用）
# ---------------------------------------------------------------------------

def get_billing_by_user(user_ids: list[str], force: bool = False) -> dict:
    """{user_id: billing dict}，60s TTL 缓存；凭证不可得时返回 {}（整体降级）。

    缓存按「全量结果」存，user_ids 子集命中缓存时直接切片；有新 id 未命中则重查全部。
    """
    ids = [u for u in user_ids if u]
    if not ids:
        return {}
    now = time.time()
    with _lock:
        cached = _cache["data"]
        fresh = (now - _cache["ts"]) < _CACHE_TTL
        if not force and fresh and all(u in cached for u in ids):
            return {u: cached[u] for u in ids}
    try:
        creds = get_frozen_credentials()
    except Exception:  # noqa: BLE001
        logger.warning("billing: credentials unavailable, degrade")
        return {}
    region = settings.aws_region

    def _one(uid: str) -> tuple[str, dict]:
        return uid, get_user_billing(uid, creds, region)

    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(ids))) as ex:
        data = dict(ex.map(_one, ids))
    with _lock:
        _cache["data"] = data
        _cache["ts"] = time.time()
    return data


def invalidate_cache() -> None:
    with _lock:
        _cache["ts"] = 0.0
        _cache["data"] = {}


# ---------------------------------------------------------------------------
# 历史月（对账 / 争议）
# ---------------------------------------------------------------------------

def month_range(year: int, month: int) -> dict:
    """ListUsagesAndLimits 要求 endTime 必须是月末 23:59:59（UTC）。"""
    if not 1 <= month <= 12:
        raise ValueError("month must be 1-12")
    last = calendar.monthrange(year, month)[1]
    start = _dt.datetime(year, month, 1, tzinfo=_dt.timezone.utc)
    end = _dt.datetime(year, month, last, 23, 59, 59, tzinfo=_dt.timezone.utc)
    return {"startTime": int(start.timestamp()), "endTime": int(end.timestamp())}


def get_user_month_usage(user_id: str, year: int, month: int) -> dict:
    """某用户某月的额度/用量/超额（历史回溯）。无数据时 has_usage_entry=False。"""
    tr = month_range(year, month)
    data = _usage_and_limits(user_id, get_frozen_credentials(), settings.aws_region, tr)
    data.update({"user_id": user_id, "year": year, "month": month})
    return data


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _num(v):
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _ts(v):
    """秒级 epoch（可能是 float）→ ISO 日期字符串；None 透传。"""
    if v is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(v), tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None
