"""端到端 API 集成测试（TestClient + moto + mock 飞书/provisioner）。

验证完整闭环：用户申请 → 推卡片 → 管理员 Web 审批 → 异步执行 → 写映射 → executed。
"""
import os
import sys
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402

REGION = settings.aws_region
MAP_TABLE = settings.mapping_table_name
REQ_TABLE = f"{settings.mapping_table_name}-requests"
GSI = settings.mapping_gsi_name

ADMIN = "ou_admin"
USER = "ou_user"


@pytest.fixture
def client(monkeypatch):
    # 配置管理员 + jwt
    monkeypatch.setattr(settings, "admin_open_ids", ADMIN)
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=MAP_TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "kiro_user_id", "AttributeType": "S"},
                {"AttributeName": "feishu_open_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "kiro_user_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{
                "IndexName": GSI,
                "KeySchema": [{"AttributeName": "feishu_open_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"}}])
        ddb.create_table(
            TableName=REQ_TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}])
        from fastapi.testclient import TestClient
        from app.main import app
        # 推审批卡片 mock 掉（不连飞书）
        with patch("app.routers.request_router.feishu.send_card", return_value="m_1"):
            yield TestClient(app)


def _auth(open_id, name):
    from app.auth import issue_token
    return {"Authorization": f"Bearer {issue_token(open_id, name)}"}


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_me_empty(client):
    r = client.get("/api/auth/me", headers=_auth(USER, "张三"))
    body = r.json()
    assert body["open_id"] == USER
    assert body["is_admin"] is False
    assert body["accounts"] == []
    assert body["suggested_username"] == "zhangsan"  # 拼音推荐


def test_unauthenticated_blocked(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/admin/requests").status_code == 401


def test_non_admin_forbidden(client):
    r = client.get("/api/admin/requests", headers=_auth(USER, "张三"))
    assert r.status_code == 403


def test_full_apply_approve_flow(client):
    """完整闭环：申请 → 管理员审批 → 执行 → 映射写入。"""
    # 1. 用户申请
    r = client.post("/api/requests/apply", headers=_auth(USER, "张三"), json={
        "username": "zhangsan", "email": "li@x.com",
        "given_name": "De", "family_name": "Li", "group": "team-a", "tier": "pro+"})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]

    # 2. 管理员看到 pending
    pend = client.get("/api/admin/requests?status=pending", headers=_auth(ADMIN, "Admin")).json()
    assert len(pend) == 1 and pend[0]["request_id"] == rid

    # 3. 审批通过（后台线程执行，mock provision + 通知）
    fake = MagicMock(success=True, user_id="test-uid-aaaa",
                     steps_succeeded=["user_created", "subscription_created"])
    with patch("app.approval.provisioner.provision", return_value=fake), \
         patch("app.approval.feishu.update_card"), \
         patch("app.approval.feishu.send_card"):
        ok = client.post(f"/api/admin/requests/{rid}/approve", headers=_auth(ADMIN, "Admin"))
        assert ok.status_code == 200
        # 等后台线程
        for _ in range(50):
            req = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()[0]
            if req["status"] == "executed":
                break
            time.sleep(0.05)
    assert req["status"] == "executed"

    # 4. 用户 me 现在能看到账号（映射已写，UserId 锚点）
    me = client.get("/api/auth/me", headers=_auth(USER, "张三")).json()
    assert len(me["accounts"]) == 1
    acct = me["accounts"][0]
    assert acct["kiro_user_id"] == "test-uid-aaaa"
    assert acct["account_role"] == "primary"
    assert acct["kiro_username"] == "zhangsan"


def test_apply_duplicate_email_blocked(client):
    """邮箱唯一校验。"""
    from app.mapping_store import AccountMapping, MappingStore
    MappingStore().put(AccountMapping(kiro_user_id="u0", feishu_open_id="ou_other",
                                      kiro_username="someone", kiro_email="dup@x.com"))
    r = client.post("/api/requests/apply", headers=_auth(USER, "王五"), json={
        "username": "wangwu", "email": "dup@x.com", "tier": "pro"})
    assert r.status_code == 400
    assert "邮箱" in r.json()["detail"]


def test_apply_no_account_quota_limit(client):
    """不再限制每人账号数：同一用户连续申请超过原配额（2）仍应被接受。"""
    from app.mapping_store import AccountMapping, MappingStore
    store = MappingStore()
    # 预置 3 个已有 active 账号（超过原 quota=2）
    for i in range(3):
        store.put(AccountMapping(kiro_user_id=f"u{i}", feishu_open_id=USER,
                                 kiro_username=f"existing{i}", kiro_email=f"e{i}@x.com",
                                 status="active"))
    # 再申请第 4 个，过去会被「已达账号配额上限」拦截，现在应放行
    r = client.post("/api/requests/apply", headers=_auth(USER, "张三"), json={
        "username": "zhangsan-extra", "email": "extra@x.com", "tier": "pro max"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"


def test_approve_dedup_via_api(client):
    """两次审批同一申请，第二次 409。"""
    r = client.post("/api/requests/apply", headers=_auth(USER, "张三"),
                    json={"username": "zhangsan", "email": "li@x.com", "tier": "pro"})
    rid = r.json()["request_id"]
    with patch("app.approval.provisioner.provision",
               return_value=MagicMock(success=True, user_id="u1", steps_succeeded=[])), \
         patch("app.approval.feishu.update_card"), patch("app.approval.feishu.send_card"):
        first = client.post(f"/api/admin/requests/{rid}/approve", headers=_auth(ADMIN, "A"))
        second = client.post(f"/api/admin/requests/{rid}/approve", headers=_auth(ADMIN, "A"))
    assert first.status_code == 200
    assert second.status_code == 409


# ---- 管理员停用 / 回收（即时执行 + 逐账号审计）----

def _seed_mapping(uid, username, role="secondary"):
    from app.mapping_store import AccountMapping, MappingStore
    MappingStore().put(AccountMapping(
        kiro_user_id=uid, feishu_open_id=USER, feishu_name="张三",
        kiro_username=username, kiro_email=f"{username}@example.com",
        tier="pro", account_role=role))


def test_deactivate_requires_admin(client):
    r = client.post("/api/admin/accounts/deactivate",
                    json={"kiro_user_ids": ["u1"]}, headers=_auth(USER, "张三"))
    assert r.status_code == 403


def test_deactivate_rejects_bad_mode(client):
    r = client.post("/api/admin/accounts/deactivate",
                    json={"kiro_user_ids": ["u1"], "mode": "whenever"},
                    headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 422


def test_deactivate_default_end_of_period_and_audited(client):
    _seed_mapping("u1", "zhangsan-2")
    with patch("app.provisioner.deactivate",
               return_value=[{"user_id": "u1", "success": True, "error": ""}]) as d, \
         patch("app.provisioner.bulk_cancel") as bc:
        r = client.post("/api/admin/accounts/deactivate",
                        json={"kiro_user_ids": ["u1"]}, headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "end_of_period" and body["recycle"] is False
    assert body["succeeded"] == 1 and body["failed"] == 0
    assert body["results"][0]["username"] == "zhangsan-2"
    d.assert_called_once_with(["u1"], "end_of_period")
    bc.assert_not_called()
    # 审计落 requests 表：type=deactivate / executed / 带 mode 与用户名
    reqs = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()
    audit = [q for q in reqs if q["type"] == "deactivate"]
    assert len(audit) == 1
    assert audit[0]["status"] == "executed"
    assert audit[0]["kiro_user_id"] == "u1"
    assert audit[0]["payload"] == {"mode": "end_of_period", "username": "zhangsan-2"}


def test_recycle_immediate_routes_to_bulk_cancel_and_marks_failures(client):
    _seed_mapping("u1", "a-2")
    _seed_mapping("u2", "b-2")
    results = [{"user_id": "u1", "success": True, "error": ""},
               {"user_id": "u2", "success": False, "error": "DeleteAssignment failed: HTTP 403"}]
    with patch("app.provisioner.bulk_cancel", return_value=results) as bc, \
         patch("app.provisioner.deactivate") as d:
        r = client.post("/api/admin/accounts/deactivate",
                        json={"kiro_user_ids": ["u1", "u2"], "mode": "immediate", "recycle": True},
                        headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded"] == 1 and body["failed"] == 1
    bc.assert_called_once_with(["u1", "u2"], "immediate")
    d.assert_not_called()
    reqs = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()
    audit = {q["kiro_user_id"]: q for q in reqs if q["type"] == "recycle"}
    assert audit["u1"]["status"] == "executed"
    assert audit["u2"]["status"] == "failed"
    assert "403" in audit["u2"]["result"]["error"]


def test_reactivate_endpoint_audited(client):
    _seed_mapping("u1", "zhangsan-2")
    with patch("app.provisioner.reactivate",
               return_value=[{"user_id": "u1", "success": True, "error": "", "tier": "pro"}]) as ra:
        r = client.post("/api/admin/accounts/reactivate",
                        json={"kiro_user_ids": ["u1"]}, headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 200
    assert r.json()["succeeded"] == 1 and r.json()["results"][0]["username"] == "zhangsan-2"
    ra.assert_called_once_with(["u1"])
    reqs = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()
    audit = [q for q in reqs if q["type"] == "reactivate"]
    assert len(audit) == 1 and audit[0]["status"] == "executed"
    assert audit[0]["payload"] == {"username": "zhangsan-2", "tier": "pro"}


# ---- 按人费用 / 额度列（Kiro Enterprise 计费直读）----

BILLING_OK = {
    "billing_synced": True, "billing_error": "", "plan_code": "KIRO_ENTERPRISE_POWER",
    "plan_start": "2026-09-19", "current_charges": 200.0, "currency": "USD",
    "account_status": "ACTIVE", "pending_plan": None, "pending_scheduled": None,
    "credit_used": 1234.5, "credit_limit": 10000.0, "credit_unit": "INVOCATIONS",
    "overage_charges": 3.2, "overage_rate": 0.04, "has_usage_entry": True,
}


def test_accounts_include_billing_fields(client):
    _seed_mapping("u1", "zhangsan", role="primary")
    _seed_mapping("u2", "zhangsan-2")
    with patch("app.billing.get_billing_by_user", return_value={"u1": BILLING_OK}) as gb, \
         patch("app.provisioner.live_subscription_map", return_value={}), \
         patch("app.usage.get_usage_by_user", return_value={}):
        r = client.get("/api/admin/accounts", headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 200
    rows = {x["kiro_user_id"]: x for x in r.json()}
    # 调用传入全部账号 id，且透传 force
    called_ids = gb.call_args.args[0]
    assert set(called_ids) == {"u1", "u2"} and gb.call_args.kwargs["force"] is False
    u1 = rows["u1"]
    assert u1["billing_synced"] is True
    assert u1["billing_plan_code"] == "KIRO_ENTERPRISE_POWER"
    assert u1["billing_current_charges"] == 200.0 and u1["billing_currency"] == "USD"
    assert u1["billing_credit_used"] == 1234.5 and u1["billing_credit_limit"] == 10000.0
    assert u1["billing_overage_charges"] == 3.2 and u1["billing_overage_rate"] == 0.04
    assert u1["billing_account_status"] == "ACTIVE"
    # 没有计费数据的账号：synced=False、字段 None，列表不中断
    u2 = rows["u2"]
    assert u2["billing_synced"] is False and u2["billing_error"] == "unavailable"
    assert u2["billing_current_charges"] is None and u2["billing_credit_limit"] is None


def test_accounts_force_passes_through_to_billing(client):
    _seed_mapping("u1", "zhangsan")
    with patch("app.billing.get_billing_by_user", return_value={}) as gb, \
         patch("app.provisioner.live_subscription_map", return_value={}), \
         patch("app.usage.get_usage_by_user", return_value={}):
        r = client.get("/api/admin/accounts?force=true", headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 200
    assert gb.call_args.kwargs["force"] is True


def test_accounts_billing_total_failure_degrades(client):
    """计费服务整体不可用（返回 {}）→ 账号列表照常，billing 字段全部 None。"""
    _seed_mapping("u1", "zhangsan")
    with patch("app.billing.get_billing_by_user", return_value={}), \
         patch("app.provisioner.live_subscription_map", return_value={}), \
         patch("app.usage.get_usage_by_user", return_value={}):
        r = client.get("/api/admin/accounts", headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 200
    assert r.json()[0]["billing_synced"] is False
    assert r.json()[0]["billing_current_charges"] is None


def test_billing_history_endpoint(client):
    hist = {"user_id": "u1", "year": 2026, "month": 10, "credit_used": 500.0,
            "credit_limit": 10000.0, "credit_unit": "INVOCATIONS",
            "overage_charges": 0.0, "overage_rate": 0.04, "has_usage_entry": True}
    with patch("app.billing.get_user_month_usage", return_value=hist) as gm:
        r = client.get("/api/admin/accounts/u1/billing-history?year=2026&month=10",
                       headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 200 and r.json()["credit_used"] == 500.0
    gm.assert_called_once_with("u1", 2026, 10)


def test_billing_history_validation_and_failure(client):
    assert client.get("/api/admin/accounts/u1/billing-history?year=2026&month=13",
                      headers=_auth(ADMIN, "Admin")).status_code == 400
    assert client.get("/api/admin/accounts/u1/billing-history?year=2026&month=10",
                      headers=_auth(USER, "张三")).status_code == 403
    with patch("app.billing.get_user_month_usage", side_effect=RuntimeError("HTTP 403")):
        r = client.get("/api/admin/accounts/u1/billing-history?year=2026&month=10",
                       headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 502 and "HTTP 403" in r.json()["detail"]
