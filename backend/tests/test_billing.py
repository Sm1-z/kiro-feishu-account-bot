"""billing 模块单测：Zorn 按用户费用/用量的扁平化、降级、缓存、历史月时间窗。"""
import datetime as _dt
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.billing as B  # noqa: E402

PLAN_RESP = {
    "appCode": "Kiro",
    "userDetails": {
        "accountStatus": "ACTIVE",
        "billingDetails": {"currentCharges": {"amount": 200.0, "currency": "USD"}},
        "currentPlan": {"planCode": "KIRO_ENTERPRISE_POWER", "startDate": 1789836719.824},
        "pendingPlan": {"planCode": "KIRO_ENTERPRISE_PRO_MAX", "scheduledDate": 1796083200},
    },
    "userId": "u1",
}
USAGE_RESP = {
    "appCode": "Kiro",
    "usagesAndLimits": [{
        "appFeatureCode": "CREDIT",
        "overageCharges": {"currentCharges": {"amount": 3.2, "currency": "USD"}},
        "pricingInfo": {"overageRate": {"amount": 0.03999999910593033, "unit": "USD"}},
        "usageMetrics": {"currentUsage": {"unit": "INVOCATIONS", "value": 1234.5},
                         "usageLimit": {"limitType": "monthly", "unit": "INVOCATIONS", "value": 10000.0}},
    }],
    "userId": "u1",
}


def _http_error(code, body):
    return urllib.error.HTTPError("https://x", code, "err", {}, MagicMock(read=lambda: body.encode()))


def _router(plan=PLAN_RESP, usage=USAGE_RESP):
    """按 X-Amz-Target 分发的假 _signed_post。"""
    def _fake(url, target, service, region, payload, credentials):
        assert service == "user-subscriptions"
        assert payload["appCode"] == "Kiro"
        if target.endswith("GetCurrentPlanDetails"):
            assert "accountId" not in payload  # 只能传 userId
            if isinstance(plan, Exception):
                raise plan
            return json.dumps(plan).encode()
        if target.endswith("ListUsagesAndLimits"):
            if isinstance(usage, Exception):
                raise usage
            _fake.last_usage_payload = payload
            return json.dumps(usage).encode()
        raise AssertionError(target)
    return _fake


@pytest.fixture(autouse=True)
def _clear_cache():
    B.invalidate_cache()
    yield
    B.invalidate_cache()


def test_get_user_billing_flattens_both_sources():
    with patch.object(B, "_signed_post", side_effect=_router()):
        r = B.get_user_billing("u1", credentials=MagicMock(), region="us-east-1")
    assert r["billing_synced"] is True and r["billing_error"] == ""
    assert r["plan_code"] == "KIRO_ENTERPRISE_POWER"
    assert r["plan_start"] == _dt.datetime.fromtimestamp(1789836719.824, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    assert r["current_charges"] == 200.0 and r["currency"] == "USD"
    assert r["account_status"] == "ACTIVE"
    assert r["pending_plan"] == "KIRO_ENTERPRISE_PRO_MAX"
    assert r["pending_scheduled"] == _dt.datetime.fromtimestamp(1796083200, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    assert r["credit_used"] == 1234.5 and r["credit_limit"] == 10000.0
    assert r["overage_charges"] == 3.2 and r["overage_rate"] == 0.04
    assert r["has_usage_entry"] is True


def test_get_user_billing_degrades_per_source():
    """套餐接口 404 → plan 字段 None，但用量半边照常；synced=False 带 error。"""
    err = _http_error(400, '{"__type":"ResourceNotFoundException","message":"Resource not found."}')
    with patch.object(B, "_signed_post", side_effect=_router(plan=err)):
        r = B.get_user_billing("u1", credentials=MagicMock(), region="us-east-1")
    assert r["billing_synced"] is False and "plan:" in r["billing_error"]
    assert r["current_charges"] is None and r["plan_code"] is None
    assert r["credit_limit"] == 10000.0


def test_usage_without_entries_marks_no_entry():
    with patch.object(B, "_signed_post", side_effect=_router(usage={"usagesAndLimits": [], "userId": "u1"})):
        r = B.get_user_billing("u1", credentials=MagicMock(), region="us-east-1")
    assert r["has_usage_entry"] is False
    assert r["credit_used"] is None and r["credit_limit"] is None
    assert r["billing_synced"] is True


def test_get_billing_by_user_caches_and_force_refreshes():
    fake = MagicMock(side_effect=_router())
    with patch.object(B, "_signed_post", fake), \
         patch.object(B, "get_frozen_credentials", return_value=MagicMock()):
        a = B.get_billing_by_user(["u1", "u2"])
        n1 = fake.call_count
        b = B.get_billing_by_user(["u1"])          # 子集命中缓存
        assert fake.call_count == n1
        c = B.get_billing_by_user(["u1", "u3"])    # 新 id 未命中 → 重查
        assert fake.call_count > n1
        B.get_billing_by_user(["u1"], force=True)  # force 绕过
    assert set(a) == {"u1", "u2"} and set(b) == {"u1"} and set(c) == {"u1", "u3"}
    assert a["u1"]["current_charges"] == 200.0


def test_get_billing_by_user_degrades_without_credentials():
    with patch.object(B, "get_frozen_credentials", side_effect=RuntimeError("no creds")):
        assert B.get_billing_by_user(["u1"]) == {}


def test_get_billing_by_user_empty_ids():
    assert B.get_billing_by_user([]) == {}
    assert B.get_billing_by_user(["", None]) == {}


def test_month_range_end_is_last_second_of_month_utc():
    tr = B.month_range(2026, 10)
    utc = _dt.timezone.utc
    assert _dt.datetime.fromtimestamp(tr["startTime"], tz=utc) == _dt.datetime(2026, 10, 1, tzinfo=utc)
    assert _dt.datetime.fromtimestamp(tr["endTime"], tz=utc) == _dt.datetime(2026, 10, 31, 23, 59, 59, tzinfo=utc)
    feb = B.month_range(2028, 2)           # 闰年
    assert feb["endTime"] - feb["startTime"] == 29 * 86400 - 1
    with pytest.raises(ValueError):
        B.month_range(2026, 13)


def test_get_user_month_usage_passes_time_range():
    fake = _router()
    with patch.object(B, "_signed_post", side_effect=fake), \
         patch.object(B, "get_frozen_credentials", return_value=MagicMock()):
        r = B.get_user_month_usage("u1", 2026, 10)
    assert fake.last_usage_payload["timeRange"] == B.month_range(2026, 10)
    assert r["year"] == 2026 and r["month"] == 10 and r["credit_limit"] == 10000.0
