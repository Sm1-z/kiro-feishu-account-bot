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
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-0123456789abcdef0123456789")
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
        # 推审批卡片 mock 掉（不连飞书）；卡片上的积分池现状也 mock 掉（不连 Kiro）
        with patch("app.routers.request_router.feishu.send_card", return_value="m_1"), \
             patch("app.routers.request_router._pool_hint", return_value=""):
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


def _wait_status(client, status="executed"):
    req = None
    for _ in range(50):
        req = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()[0]
        if req["status"] == status:
            break
        time.sleep(0.05)
    return req


def test_full_apply_approve_flow(client):
    """完整闭环：申请（带上限）→ 管理员审批 → 执行（入池）→ 映射写入 → 我的账号显示池实况。"""
    r = client.post("/api/requests/apply", headers=_auth(USER, "张三"), json={
        "username": "zhangsan", "email": "li@x.com", "given_name": "De", "family_name": "Li",
        "group": "team-a", "usage_limit": 800, "reason": "做大型重构"})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]

    pend = client.get("/api/admin/requests?status=pending", headers=_auth(ADMIN, "Admin")).json()
    assert len(pend) == 1 and pend[0]["request_id"] == rid
    assert pend[0]["payload"]["usage_limit"] == 800

    fake = MagicMock(success=True, user_id="test-uid-aaaa", warning="",
                     steps_succeeded=["user_created", "pool_joined", "seat_auto", "limit_set"])
    with patch("app.approval.provisioner.provision", return_value=fake) as prov, \
         patch("app.approval.feishu.update_card"), \
         patch("app.approval.feishu.send_card"):
        ok = client.post(f"/api/admin/requests/{rid}/approve", headers=_auth(ADMIN, "Admin"))
        assert ok.status_code == 200
        req = _wait_status(client)
    assert req["status"] == "executed"
    assert prov.call_args.kwargs["usage_limit"] == 800

    with patch("app.provisioner.live_status_map",
               return_value={"test-uid-aaaa": {"status": "SEATED", "direct": True}}), \
         patch("app.pool.effective_limits", return_value={
             "test-uid-aaaa": {"limit": 800.0, "scope": "USER", "scope_id": "test-uid-aaaa"}}), \
         patch("app.usage.get_current_month_usage_by_user",
               return_value={"test-uid-aaaa": {"credits": 12.5}}):
        me = client.get("/api/auth/me", headers=_auth(USER, "张三")).json()
    acct = me["accounts"][0]
    assert acct["kiro_user_id"] == "test-uid-aaaa" and acct["account_role"] == "primary"
    assert acct["pool_status"] == "SEATED"
    assert acct["limit"] == 800.0 and acct["limit_scope"] == "USER"
    assert acct["month_credits"] == 12.5 and acct["usage_limit"] == 800


def test_apply_rejects_invalid_limit(client):
    r = client.post("/api/requests/apply", headers=_auth(USER, "张三"), json={
        "username": "zhangsan", "email": "li@x.com", "usage_limit": 0})
    assert r.status_code == 422


def test_apply_duplicate_email_blocked(client):
    """邮箱唯一校验。"""
    from app.mapping_store import AccountMapping, MappingStore
    MappingStore().put(AccountMapping(kiro_user_id="u0", feishu_open_id="ou_other",
                                      kiro_username="someone", kiro_email="dup@x.com"))
    r = client.post("/api/requests/apply", headers=_auth(USER, "王五"), json={
        "username": "wangwu", "email": "dup@x.com"})
    assert r.status_code == 400
    assert "邮箱" in r.json()["detail"]


def test_apply_blocked_when_user_has_account(client):
    """每人一个账号：已有账号（包括已停用的）不能再申请。"""
    from app.mapping_store import AccountMapping, MappingStore
    MappingStore().put(AccountMapping(kiro_user_id="u0", feishu_open_id=USER, kiro_username="zhangsan",
                                      kiro_email="zs@x.com", status="cancelled"))
    r = client.post("/api/requests/apply", headers=_auth(USER, "张三"), json={
        "username": "zhangsan-extra", "email": "extra@x.com"})
    assert r.status_code == 400 and "每人只能有一个账号" in r.json()["detail"]


def test_apply_blocked_when_apply_pending(client):
    ok = client.post("/api/requests/apply", headers=_auth(USER, "张三"),
                     json={"username": "zhangsan", "email": "li@x.com"})
    assert ok.status_code == 200
    again = client.post("/api/requests/apply", headers=_auth(USER, "张三"),
                        json={"username": "zhangsan2", "email": "li2@x.com"})
    assert again.status_code == 400 and "处理中" in again.json()["detail"]


def test_admin_manual_link_blocks_second_account(client):
    from app.mapping_store import AccountMapping, MappingStore
    MappingStore().put(AccountMapping(kiro_user_id="u0", feishu_open_id=USER, kiro_username="zhangsan"))
    r = client.post("/api/admin/link", headers=_auth(ADMIN, "Admin"), json={
        "kiro_user_id": "u9", "feishu_open_id": USER, "kiro_username": "zhangsan-legacy"})
    assert r.status_code == 400 and "每人只能有一个账号" in r.json()["detail"]


def test_approve_dedup_via_api(client):
    """两次审批同一申请，第二次 409。"""
    r = client.post("/api/requests/apply", headers=_auth(USER, "张三"),
                    json={"username": "zhangsan", "email": "li@x.com"})
    rid = r.json()["request_id"]
    with patch("app.approval.provisioner.provision",
               return_value=MagicMock(success=True, user_id="u1", warning="", steps_succeeded=[])), \
         patch("app.approval.feishu.update_card"), patch("app.approval.feishu.send_card"):
        first = client.post(f"/api/admin/requests/{rid}/approve", headers=_auth(ADMIN, "A"))
        second = client.post(f"/api/admin/requests/{rid}/approve", headers=_auth(ADMIN, "A"))
    assert first.status_code == 200
    assert second.status_code == 409


def _seed_mapping(uid, username, role="secondary", owner=USER, usage_limit=None):
    from app.mapping_store import AccountMapping, MappingStore
    MappingStore().put(AccountMapping(
        kiro_user_id=uid, feishu_open_id=owner, feishu_name="张三",
        kiro_username=username, kiro_email=f"{username}@example.com",
        usage_limit=usage_limit, account_role=role))


# ---- 申请调整上限 ----

def test_limit_change_request_and_approve(client):
    _seed_mapping("u1", "zhangsan", role="primary", usage_limit=500)
    with patch("app.pool.effective_limit",
               return_value={"limit": 500.0, "scope": "USER", "scope_id": "u1"}):
        r = client.post("/api/requests/limit-change", headers=_auth(USER, "张三"),
                        json={"kiro_user_id": "u1", "requested_limit": 2000, "reason": "月底赶工"})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    req = client.get("/api/admin/requests?status=pending", headers=_auth(ADMIN, "A")).json()[0]
    assert req["type"] == "limit_change"
    assert req["payload"]["current_limit"] == 500 and req["payload"]["requested_limit"] == 2000

    with patch("app.approval.provisioner.set_user_limit",
               return_value=MagicMock(success=True)) as setl, \
         patch("app.approval.feishu.update_card"), patch("app.approval.feishu.send_card"):
        client.post(f"/api/admin/requests/{rid}/approve", headers=_auth(ADMIN, "A"))
        done = _wait_status(client)
    assert done["status"] == "executed"
    setl.assert_called_once_with("u1", 2000)


def test_limit_change_rejects_foreign_account(client):
    _seed_mapping("u1", "lisi", owner="ou_other")
    r = client.post("/api/requests/limit-change", headers=_auth(USER, "张三"),
                    json={"kiro_user_id": "u1", "requested_limit": 2000})
    assert r.status_code == 404


# ---- 管理员停用 / 回收 / 恢复（即时执行 + 逐账号审计）----

def test_deactivate_requires_admin(client):
    r = client.post("/api/admin/accounts/deactivate",
                    json={"kiro_user_ids": ["u1"]}, headers=_auth(USER, "张三"))
    assert r.status_code == 403


def test_deactivate_removes_from_pool_and_audited(client):
    _seed_mapping("u1", "zhangsan-2")
    with patch("app.provisioner.deactivate",
               return_value=[{"user_id": "u1", "success": True, "error": ""}]) as d, \
         patch("app.provisioner.bulk_cancel") as bc:
        r = client.post("/api/admin/accounts/deactivate",
                        json={"kiro_user_ids": ["u1"]}, headers=_auth(ADMIN, "Admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["recycle"] is False and body["succeeded"] == 1
    assert body["results"][0]["username"] == "zhangsan-2"
    d.assert_called_once_with(["u1"])
    bc.assert_not_called()
    reqs = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()
    audit = [q for q in reqs if q["type"] == "deactivate"]
    assert len(audit) == 1 and audit[0]["status"] == "executed"
    assert audit[0]["kiro_user_id"] == "u1"
    assert audit[0]["payload"] == {"username": "zhangsan-2"}


def test_recycle_routes_to_bulk_cancel_and_marks_failures(client):
    _seed_mapping("u1", "a-2")
    _seed_mapping("u2", "b-2")
    results = [{"user_id": "u1", "success": True, "error": ""},
               {"user_id": "u2", "success": False, "error": "DeleteAssignment failed: HTTP 403"}]
    with patch("app.provisioner.bulk_cancel", return_value=results) as bc, \
         patch("app.provisioner.deactivate") as d:
        r = client.post("/api/admin/accounts/deactivate",
                        json={"kiro_user_ids": ["u1", "u2"], "recycle": True},
                        headers=_auth(ADMIN, "Admin"))
    body = r.json()
    assert body["succeeded"] == 1 and body["failed"] == 1
    bc.assert_called_once_with(["u1", "u2"])
    d.assert_not_called()
    reqs = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()
    audit = {q["kiro_user_id"]: q for q in reqs if q["type"] == "recycle"}
    assert audit["u1"]["status"] == "executed"
    assert audit["u2"]["status"] == "failed" and "403" in audit["u2"]["result"]["error"]


def test_reactivate_and_retry_seats_audited(client):
    _seed_mapping("u1", "zhangsan-2")
    with patch("app.provisioner.reactivate", return_value=[
            {"user_id": "u1", "success": True, "error": "", "warning": "没有空余席位"}]):
        r = client.post("/api/admin/accounts/reactivate",
                        json={"kiro_user_ids": ["u1"]}, headers=_auth(ADMIN, "Admin"))
    assert r.json()["succeeded"] == 1
    with patch("app.provisioner.retry_seats", return_value=[
            {"user_id": "u1", "success": True, "error": ""}]) as rs:
        r2 = client.post("/api/admin/accounts/retry-seats",
                         json={"kiro_user_ids": ["u1"]}, headers=_auth(ADMIN, "Admin"))
    assert r2.json()["succeeded"] == 1
    rs.assert_called_once_with(["u1"])
    reqs = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()
    re_audit = [q for q in reqs if q["type"] == "reactivate"][0]
    assert re_audit["result"] == {"ok": True, "warning": "没有空余席位"}
    assert [q for q in reqs if q["type"] == "seat_retry"][0]["status"] == "executed"


# ---- 账号总览（积分池实况 + 生效上限 + 本月用量）----

MONTH_USAGE = {"u1": {"credits": 400.0, "overage": 0.0, "messages": 73, "active_days": 3,
                      "last_active": "2026-11-26"}}


def _accounts(client, live=None, limits=None, month=None, force=False):
    with patch("app.provisioner.live_status_map", return_value=live or {}) as lv, \
         patch("app.pool.effective_limits", return_value=limits or {}) as el, \
         patch("app.usage.get_usage_by_user", return_value={}), \
         patch("app.usage.get_current_month_usage_by_user", return_value=month or {}) as gm:
        r = client.get(f"/api/admin/accounts{'?force=true' if force else ''}",
                       headers=_auth(ADMIN, "Admin"))
    return r, lv, el, gm


def test_accounts_include_pool_fields(client):
    _seed_mapping("u1", "zhangsan", role="primary", usage_limit=1000)
    _seed_mapping("u2", "zhangsan-2")
    _seed_mapping("u3", "zhangsan-3")
    r, *_ = _accounts(client,
                      live={"u1": {"status": "SEATED", "direct": True},
                            "u2": {"status": "AWAITING_SEAT", "direct": True}},
                      limits={"u1": {"limit": 1000.0, "scope": "USER", "scope_id": "u1"},
                              "u2": None},
                      month=MONTH_USAGE)
    assert r.status_code == 200
    rows = {x["kiro_user_id"]: x for x in r.json()}
    u1, u2, u3 = rows["u1"], rows["u2"], rows["u3"]
    assert u1["pool_status"] == "SEATED" and u1["limit"] == 1000.0 and u1["limit_scope"] == "USER"
    assert u1["month_credits"] == 400.0 and u1["limit_used_pct"] == 40.0
    assert u1["usage_limit"] == 1000
    assert u2["pool_status"] == "AWAITING_SEAT"
    assert u2["limit"] is None and u2["limit_scope"] == "NONE"  # 无上限
    assert u2["month_credits"] == 0.0  # 报表可用但该用户本月没用
    assert u3["pool_status"] is None  # 不在池里
    assert u3["limit_scope"] is None  # 查询失败 / 未知
    assert "billing_current_charges" not in u1 and "tier" not in u1


def test_accounts_degrade_when_sources_fail(client):
    _seed_mapping("u1", "zhangsan", role="primary")
    with patch("app.provisioner.live_status_map", side_effect=RuntimeError("down")), \
         patch("app.pool.effective_limits", side_effect=RuntimeError("down")), \
         patch("app.usage.get_usage_by_user", return_value={}), \
         patch("app.usage.get_current_month_usage_by_user", return_value={}):
        r = client.get("/api/admin/accounts", headers=_auth(ADMIN, "Admin"))
    row = r.json()[0]
    assert row["live_synced"] is False and row["pool_status"] is None
    assert row["limit"] is None and row["month_credits"] is None


def test_accounts_force_passes_through(client):
    _seed_mapping("u1", "zhangsan", role="primary")
    _, lv, el, gm = _accounts(client, force=True)
    lv.assert_called_once_with(force=True)
    assert el.call_args.kwargs["force"] is True
    gm.assert_called_once_with(force=True)


def test_usage_history_endpoint(client):
    month = {"u1": {"credits": 512.3, "overage": 0.0, "messages": 120, "active_days": 9,
                    "last_active": "2026-10-30"}}
    with patch("app.usage._enabled", return_value=True), \
         patch("app.usage.get_month_usage_by_user", return_value=month) as gm:
        r = client.get("/api/admin/accounts/u1/usage-history?year=2026&month=10",
                       headers=_auth(ADMIN, "Admin"))
    body = r.json()
    gm.assert_called_once_with(2026, 10)
    assert body["available"] is True and body["has_usage_entry"] is True
    assert body["credit_used"] == 512.3 and body["active_days"] == 9
    assert client.get("/api/admin/accounts/u1/usage-history?year=2026&month=13",
                      headers=_auth(ADMIN, "Admin")).status_code == 400


# ---- 积分池总览 / 上限管理 ----

def test_pool_overview_degrades_per_part(client):
    from app.pool import PoolContext
    ctx = PoolContext("111122223333", "us-east-1", "arn:i", "arn:p", "POSTPAID")
    with patch("app.pool.get_context", return_value=ctx), \
         patch("app.pool.overview", return_value={"total_left": 253.0}), \
         patch("app.pool.seat_status", return_value={"users": {"u1": {}}, "seated": 1}), \
         patch("app.pool.profile_limit", side_effect=RuntimeError("limits down")), \
         patch("app.pool.list_credit_purchases", return_value=[]), \
         patch("app.quotas.get_kiro_quotas", return_value={"onboarding_cap": None}):
        body = client.get("/api/admin/pool", headers=_auth(ADMIN, "Admin")).json()
    assert body["context"]["payment_model"] == "POSTPAID"
    assert body["balance"]["total_left"] == 253.0
    assert body["seats"] == {"seated": 1}  # 不把每个用户的明细带出去
    assert body["profile_limit"] is None
    assert any("profile_limit" in e for e in body["errors"])


def test_pool_overview_requires_admin(client):
    assert client.get("/api/admin/pool", headers=_auth(USER, "张三")).status_code == 403


def test_set_limit_user_audited_and_snapshot(client):
    _seed_mapping("u1", "zhangsan", role="primary")
    with patch("app.pool.effective_limit", return_value=None), \
         patch("app.pool.set_limit") as setl:
        r = client.post("/api/admin/limits", headers=_auth(ADMIN, "Admin"),
                        json={"scope": "USER", "scope_id": "u1", "usage_limit": 300})
    assert r.status_code == 200, r.text
    setl.assert_called_once_with("USER", "u1", 300)
    from app.mapping_store import MappingStore
    assert MappingStore().get("u1").usage_limit == 300
    reqs = client.get("/api/admin/requests", headers=_auth(ADMIN, "Admin")).json()
    audit = [q for q in reqs if q["type"] == "limit_set"][0]
    assert audit["payload"]["to"] == "300" and audit["payload"]["username"] == "zhangsan"


def test_set_limit_pool_remove_and_validation(client):
    from app.pool import PoolError
    with patch("app.pool.profile_limit", return_value=150.0), \
         patch("app.pool.set_limit") as setl:
        r = client.post("/api/admin/limits", headers=_auth(ADMIN, "Admin"),
                        json={"scope": "POOL", "usage_limit": None})
    assert r.status_code == 200
    setl.assert_called_once_with("POOL", "", None)
    with patch("app.pool.profile_limit", return_value=None), \
         patch("app.pool.set_limit", side_effect=PoolError("超过了积分池当前的 credits 总量")):
        r2 = client.post("/api/admin/limits", headers=_auth(ADMIN, "Admin"),
                         json={"scope": "POOL", "usage_limit": 99999})
    assert r2.status_code == 400 and "credits 总量" in r2.json()["detail"]
    r3 = client.post("/api/admin/limits", headers=_auth(ADMIN, "Admin"),
                     json={"scope": "USER", "usage_limit": 10})
    assert r3.status_code == 400  # USER 缺 scope_id
    r4 = client.post("/api/admin/limits", headers=_auth(ADMIN, "Admin"),
                     json={"scope": "GROUP", "scope_id": "g1", "usage_limit": 10})
    assert r4.status_code == 422  # 组上限暂不开放


# ---- 安全回归 ----

def test_static_resolver_blocks_traversal(tmp_path):
    from app.main import resolve_static
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("ok")
    (tmp_path / ".env").write_text("FEISHU_APP_SECRET=leak")
    assert resolve_static(str(root), "index.html") == str((root / "index.html").resolve())
    assert resolve_static(str(root), "../.env") is None
    assert resolve_static(str(root), "assets/../../.env") is None
    assert resolve_static(str(root), "/etc/hosts") is None
    assert resolve_static(str(root), "missing.js") is None
    assert resolve_static(str(root), "") is None


def test_short_jwt_secret_fails_closed(client, monkeypatch):
    token_headers = _auth(USER, "张三")  # 用合法密钥签出的 token
    monkeypatch.setattr(settings, "jwt_secret", "")
    assert client.get("/api/auth/me", headers=token_headers).status_code == 503
    from fastapi import HTTPException
    from app.auth import issue_token
    with pytest.raises(HTTPException):
        issue_token(USER, "张三")  # 空密钥不签发


def test_oauth_callback_requires_matching_state_cookie(client):
    with patch("app.routers.auth_router.feishu.oauth_exchange_user",
               return_value={"open_id": USER, "name": "张三"}) as ex:
        # 没有 cookie：拒绝，且不去换 code
        r = client.get("/api/auth/feishu/callback?code=c1&state=s1", follow_redirects=False)
        assert r.status_code == 400
        ex.assert_not_called()
        # cookie 与 state 不一致：拒绝
        client.cookies.set("kiro_oauth_state", "other", path="/api/auth/feishu")
        assert client.get("/api/auth/feishu/callback?code=c1&state=s1",
                          follow_redirects=False).status_code == 400
        # 一致：发 token，token 只出现在 fragment 里
        client.cookies.clear()
        client.cookies.set("kiro_oauth_state", "s1", path="/api/auth/feishu")
        ok = client.get("/api/auth/feishu/callback?code=c1&state=s1", follow_redirects=False)
    assert ok.status_code in (302, 307)
    loc = ok.headers["location"]
    assert "/auth/callback#token=" in loc and "?token=" not in loc


def test_oauth_login_sets_state_cookie(client):
    with patch("app.routers.auth_router.feishu.oauth_authorize_url",
               side_effect=lambda st: f"https://open.feishu.cn/authorize?state={st}"):
        r = client.get("/api/auth/feishu/login", follow_redirects=False)
    cookie = r.headers.get("set-cookie", "")
    assert "kiro_oauth_state=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    state = cookie.split("kiro_oauth_state=")[1].split(";")[0]
    assert r.headers["location"].endswith(f"state={state}")
