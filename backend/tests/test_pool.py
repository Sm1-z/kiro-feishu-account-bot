"""积分池领域层测试：成员幂等、席位（预付 / 后付）、上限校验、余额汇总。

接口返回结构取自 2026-11 在真实后付账号上的实测响应。
"""
import dataclasses
import datetime as dt
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import pool as P  # noqa: E402
from app.kiro_api import KiroApiError  # noqa: E402

POSTPAID = P.PoolContext("111122223333", "us-east-1", "arn:aws:sso:::instance/ssoins-x",
                         "arn:aws:sso::111122223333:application/ssoins-x/apl-y", "POSTPAID")
PREPAID = dataclasses.replace(POSTPAID, payment_model="PREPAID")


@pytest.fixture(autouse=True)
def _reset_cache():
    P._cache.clear()
    yield
    P._cache.clear()


@pytest.fixture
def postpaid():
    with patch.object(P, "get_context", return_value=POSTPAID):
        yield


@pytest.fixture
def prepaid():
    with patch.object(P, "get_context", return_value=PREPAID):
        yield


def _err(kind, msg=""):
    return KiroApiError("Op", 400, kind, msg)


# ---- 成员 ----

def test_add_member_payload_and_new(postpaid):
    with patch("app.pool.kiro_api.cps", return_value={}) as cps:
        assert P.add_member("u1") is True
    op, payload = cps.call_args.args
    assert op == "CreateAssignment"
    assert payload == {"principalId": "u1", "principalType": "USER", "subscriptionType": "CREDIT_POOL"}


def test_add_member_conflict_already_member_is_idempotent(postpaid):
    def fake(op, payload=None, **k):
        if op == "CreateAssignment":
            raise _err("conflict", "already assigned")
        return {"assignments": [{"principalId": "u1", "principalType": "USER"}]}

    with patch("app.pool.kiro_api.cps", side_effect=fake):
        assert P.add_member("u1") is False


def test_add_member_conflict_not_member_raises(postpaid):
    def fake(op, payload=None, **k):
        if op == "CreateAssignment":
            raise _err("conflict", "something else")
        return {"assignments": []}

    with patch("app.pool.kiro_api.cps", side_effect=fake):
        with pytest.raises(P.PoolError):
            P.add_member("u1")


def test_add_member_prepaid_before_first_purchase_readable(prepaid):
    msg = ("Your credit pool is not yet ready for seat assignments. "
           "Complete your seat purchase and try again.")
    with patch("app.pool.kiro_api.cps", side_effect=_err("conflict", msg)):
        with pytest.raises(P.PoolError, match="预付积分池还没有购买席位"):
            P.add_member("u1")


def test_add_member_onboarding_cap_readable(postpaid):
    with patch("app.pool.kiro_api.cps",
               side_effect=_err("validation", "USER_ONBOARDING_CAP_EXCEEDED")):
        with pytest.raises(P.PoolError, match="可接入用户数上限"):
            P.add_member("u1")


def test_remove_member_payload_and_not_found_idempotent(postpaid):
    with patch("app.pool.kiro_api.cps", side_effect=_err("not_found")) as cps:
        P.remove_member("u1")  # 不抛
    op, payload = cps.call_args.args
    assert op == "DeleteAssignment"
    assert payload["subscriptionType"] == "CREDIT_POOL"
    assert payload["deletionMode"] == "IMMEDIATE"


def test_remove_member_other_errors_raise(postpaid):
    with patch("app.pool.kiro_api.cps", side_effect=_err("access_denied")):
        with pytest.raises(KiroApiError):
            P.remove_member("u1")


# ---- 席位 ----

def test_assign_seat_postpaid_is_automatic(postpaid):
    with patch("app.pool.kiro_api.cps") as cps:
        assert P.assign_seat("u1") == (True, "AUTO")
    cps.assert_not_called()


def test_assign_seat_prepaid_success_and_capacity(prepaid):
    with patch("app.pool.kiro_api.cps",
               return_value={"successfulAssignments": [{"userId": "u1"}]}):
        assert P.assign_seat("u1") == (True, "")
    with patch("app.pool.kiro_api.cps", return_value={
            "failedAssignments": [{"userId": "u1", "reason": "CAPACITY_EXHAUSTED"}]}):
        assert P.assign_seat("u1") == (False, "CAPACITY_EXHAUSTED")


def test_assign_seat_prepaid_retries_not_eligible(prepaid):
    seq = [{"failedAssignments": [{"userId": "u1", "reason": "NOT_ELIGIBLE"}]},
           {"successfulAssignments": [{"userId": "u1"}]}]
    with patch("app.pool.kiro_api.cps", side_effect=seq), patch("app.pool.time.sleep"):
        assert P.assign_seat("u1") == (True, "")


def test_seat_status_plan_code_and_mapping(prepaid):
    resp = {"activeSeats": 1, "inactiveUsers": 1, "totalSeats": 3, "availableSeats": 2,
            "items": [{"userId": "u1", "status": "ACTIVE", "activatedAt": 1.0},
                      {"userId": "u2", "status": "INACTIVE"}]}
    with patch("app.pool.kiro_api.zorn", return_value=resp) as z:
        s = P.seat_status()
    assert z.call_args.args[1]["planCode"] == "KIRO_POOLING_SEAT_PREPAID"
    assert s["users"]["u1"]["status"] == P.SEATED
    assert s["users"]["u2"]["status"] == P.AWAITING_SEAT
    assert (s["seated"], s["awaiting"], s["total"], s["available"]) == (1, 1, 3, 2)


def test_seat_status_postpaid_plan_code(postpaid):
    with patch("app.pool.kiro_api.zorn", return_value={"activeSeats": 0, "items": []}) as z:
        P.seat_status()
    assert z.call_args.args[1]["planCode"] == "KIRO_POOLING_SEAT"


def test_member_status_map_merges_direct_members(postpaid):
    with patch.object(P, "seat_status", return_value={"users": {
            "u1": {"status": P.SEATED, "activated_at": 1.0},
            "g1-member": {"status": P.SEATED, "activated_at": 2.0}}}), \
         patch.object(P, "list_direct_members", return_value={"users": {"u1", "u3"},
                                                               "groups": {"g1"}}):
        m = P.member_status_map()
    assert m["u1"] == {"status": P.SEATED, "direct": True, "activated_at": 1.0}
    assert m["g1-member"]["direct"] is False  # 经组加入
    assert m["u3"]["status"] == P.AWAITING_SEAT  # 直接加入但席位列表里还没有


# ---- 上限 ----

def test_set_limit_validates_scope_and_value(postpaid):
    with pytest.raises(P.PoolError):
        P.set_limit("TEAM", "x", 10)
    with pytest.raises(P.PoolError):
        P.set_limit("USER", "u1", -1)
    with pytest.raises(P.PoolError):
        P.set_limit("USER", "", 10)


def test_set_limit_rejects_above_pool_total(postpaid):
    with patch.object(P, "overview", return_value={"total_quantity": 253.0}), \
         patch("app.pool.kiro_api.cps") as cps:
        with pytest.raises(P.PoolError, match="credits 总量"):
            P.set_limit("USER", "u1", 300)
    cps.assert_not_called()


def test_set_limit_pool_scope_uses_account_and_remove_omits_value(postpaid):
    with patch.object(P, "overview", return_value={"total_quantity": 253.0}), \
         patch("app.pool.kiro_api.cps", return_value={}) as cps:
        P.set_limit("POOL", "ignored", 150)
        P.set_limit("USER", "u1", None)
    set_pool = cps.call_args_list[0].args[1]
    assert set_pool["scope"] == "POOL" and set_pool["scopeId"] == "111122223333"
    assert set_pool["usageLimit"] == 150.0 and set_pool["resourceType"] == "CREDIT"
    remove = cps.call_args_list[1].args[1]
    assert "usageLimit" not in remove and remove["scopeId"] == "u1"
    assert set_pool["clientToken"] != remove["clientToken"]


def test_set_limit_zero_allowed_and_skips_check_when_balance_unavailable(postpaid):
    with patch.object(P, "overview", side_effect=RuntimeError("down")), \
         patch("app.pool.kiro_api.cps", return_value={}) as cps:
        P.set_limit("USER", "u1", 0)
    assert cps.call_args.args[1]["usageLimit"] == 0.0


def test_effective_limit_none_and_value(postpaid):
    with patch("app.pool.kiro_api.zorn", return_value={}):
        assert P.effective_limit("u1") is None
    with patch("app.pool.kiro_api.zorn", return_value={
            "effectiveLimit": 200.0, "source": {"scope": "GROUP", "scopeId": "g1"}}):
        assert P.effective_limit("u1") == {"limit": 200.0, "scope": "GROUP", "scope_id": "g1"}


def test_effective_limits_omits_failures(postpaid):
    def fake(uid):
        if uid == "bad":
            raise RuntimeError("x")
        return None if uid == "free" else {"limit": 100.0, "scope": "USER", "scope_id": uid}

    with patch.object(P, "effective_limit", side_effect=fake):
        out = P.effective_limits(["u1", "free", "bad"])
    assert out["u1"]["limit"] == 100.0
    assert "free" in out and out["free"] is None
    assert "bad" not in out


def test_profile_limit_from_list(postpaid):
    with patch("app.pool.kiro_api.cps", return_value={"usageLimits": [
            {"scope": "USER", "scopeId": "u1", "usageLimit": 100.0},
            {"scope": "POOL", "scopeId": "111122223333", "usageLimit": 150.0}]}):
        assert P.profile_limit() == 150.0


# ---- 余额汇总 ----

REAL_ITEMS = [  # 2026-11-27 实测：2 个席位（首月折算）+ 125 追加 credits
    {"createdAt": 1795752088.7, "effectiveAt": 1795752088.7, "entitlementType": "POOL_SUBSCRIPTION",
     "details": {"poolSubscription": {"quotaPerSeat": 500.0, "seatCount": 2, "usage": {
         "currentUsage": 0.0, "quantity": 128.0, "resourceType": "CREDIT"}}}},
    {"createdAt": 1795752140.4, "effectiveAt": 1795752132.9, "expiresAt": 1827288132.9,
     "entitlementId": "d1cb", "entitlementType": "ADDON",
     "details": {"addon": {"source": "MANUAL", "usage": {
         "currentUsage": 0.0, "quantity": 125.0, "resourceType": "CREDIT"}}}},
]


def test_summarize_real_response():
    now = dt.datetime(2026, 11, 27, 6, 0, tzinfo=dt.timezone.utc)
    s = P.summarize(REAL_ITEMS, POSTPAID, now)
    assert s["monthly"]["quantity"] == 128.0 and s["monthly"]["seat_count"] == 2
    assert s["addon"]["left"] == 125.0 and s["addon"]["expiring_this_cycle"] == 0
    assert s["total_quantity"] == 253.0 and s["total_left"] == 253.0
    assert s["avg_daily_used"] == 0 and s["pool_runout_at"] is None
    assert s["seat_fee_full_month_usd"] == 40.0
    assert s["cycle_end"].startswith("2026-12-01")
    assert s["addon"]["items"][0]["expires_at"].startswith("2027-11-27")


def test_summarize_burn_projection():
    start = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    now = start + dt.timedelta(days=10)
    items = [
        {"effectiveAt": start.timestamp(), "entitlementType": "POOL_SUBSCRIPTION",
         "details": {"poolSubscription": {"quotaPerSeat": 500.0, "seatCount": 2,
                                          "usage": {"currentUsage": 600.0, "quantity": 1000.0}}}},
        {"effectiveAt": start.timestamp(), "expiresAt": (start + dt.timedelta(days=20)).timestamp(),
         "entitlementType": "ADDON",
         "details": {"addon": {"usage": {"currentUsage": 0.0, "quantity": 500.0}}}},
    ]
    s = P.summarize(items, POSTPAID, now)
    assert s["avg_daily_used"] == 60.0
    # 月度剩 400 → 6.7 天后用完；加上追加 500 → 15 天后池子耗尽（账期还剩 20 天）
    assert s["monthly_runout_at"].startswith("2026-11-17")
    assert s["pool_runout_at"].startswith("2026-11-26")
    assert s["addon"]["expiring_this_cycle"] == 500.0
