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
