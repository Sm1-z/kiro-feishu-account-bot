"""审批执行引擎测试。

moto mock 两张表 + mock provisioner/feishu。验证端到端：
申请 → 抢占审批(防重) → 执行开通 → 写映射(UserId锚点) → 状态 executed。
"""
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.mapping_store import MappingStore, PRIMARY, SECONDARY  # noqa: E402
from app.request_store import (  # noqa: E402
    APPLY, APPROVED, EXECUTED, FAILED, PENDING, UPGRADE, Request, RequestStore,
)

REGION = settings.aws_region
MAP_TABLE = "kiro-account-mapping"
REQ_TABLE = "kiro-account-mapping-requests"
GSI = "feishu_open_id-index"


@pytest.fixture
def stores():
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
                "Projection": {"ProjectionType": "ALL"}}],
        )
        ddb.create_table(
            TableName=REQ_TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
        )
        from app.approval import ApprovalService
        svc = ApprovalService(
            requests=RequestStore(table_name=REQ_TABLE),
            mapping=MappingStore(table_name=MAP_TABLE, gsi_name=GSI),
        )
        yield svc


def _apply_req(svc, **payload):
    p = {"username": "zhangsan", "email": "l@x.com", "given_name": "De",
         "family_name": "Li", "tier": "pro+", "group": "team-a"}
    p.update(payload)
    return svc.requests.create(Request(user_open_id="ou_li", user_name="张三",
                                       type=APPLY, payload=p))


def test_claim_approve_dedup(stores):
    """两个管理员同时点通过，只有一个抢到。"""
    svc = stores
    req = _apply_req(svc)
    assert svc.claim_approve(req.request_id, "ou_admin1") is True
    assert svc.claim_approve(req.request_id, "ou_admin2") is False


def test_execute_apply_success_writes_mapping(stores):
    """开通成功 → 写映射(UserId锚点, 首个=primary) → executed。"""
    svc = stores
    req = _apply_req(svc)
    svc.claim_approve(req.request_id, "ou_admin")

    fake = MagicMock(success=True, user_id="test-uid-aaaa",
                     steps_succeeded=["user_created", "subscription_created"])
    with patch("app.approval.provisioner.provision", return_value=fake):
        done = svc.execute_approved(req.request_id)

    assert done.status == EXECUTED
    assert done.result["user_id"] == "test-uid-aaaa"
    # 映射已写入，UserId 为锚点，首账号为 primary
    m = svc.mapping.get("test-uid-aaaa")
    assert m is not None
    assert m.feishu_open_id == "ou_li"
    assert m.account_role == PRIMARY
    assert m.kiro_username == "zhangsan"


def test_second_account_is_secondary(stores):
    """同人第二个账号 → secondary。"""
    svc = stores
    # 先开通主账号
    r1 = _apply_req(svc, username="zhangsan")
    svc.claim_approve(r1.request_id, "ou_admin")
    with patch("app.approval.provisioner.provision",
               return_value=MagicMock(success=True, user_id="u1", steps_succeeded=[])):
        svc.execute_approved(r1.request_id)
    # 再开通副账号
    r2 = _apply_req(svc, username="zhangsan-new1", email="l2@x.com")
    svc.claim_approve(r2.request_id, "ou_admin")
    with patch("app.approval.provisioner.provision",
               return_value=MagicMock(success=True, user_id="u2", steps_succeeded=[])):
        svc.execute_approved(r2.request_id)

    assert svc.mapping.get("u1").account_role == PRIMARY
    assert svc.mapping.get("u2").account_role == SECONDARY


def test_execute_apply_failure_no_mapping(stores):
    """开通失败 → status=failed，不写映射。"""
    svc = stores
    req = _apply_req(svc)
    svc.claim_approve(req.request_id, "ou_admin")
    fake = MagicMock(success=False, user_id="", error="组不存在", error_step="add_to_group")
    with patch("app.approval.provisioner.provision", return_value=fake):
        done = svc.execute_approved(req.request_id)
    assert done.status == FAILED
    assert "组不存在" in done.result["error"]
    # 无任何映射写入
    assert svc.mapping.all_usernames() == set()


def test_execute_upgrade_updates_tier(stores):
    """升级成功 → 同步映射 tier。"""
    svc = stores
    # 先有一个账号映射
    from app.mapping_store import AccountMapping
    svc.mapping.put(AccountMapping(kiro_user_id="u1", feishu_open_id="ou_li",
                                   kiro_username="zhangsan", tier="pro", account_role=PRIMARY))
    req = svc.requests.create(Request(
        user_open_id="ou_li", user_name="张三", type=UPGRADE,
        kiro_user_id="u1", payload={"username": "zhangsan", "target_tier": "power"}))
    svc.claim_approve(req.request_id, "ou_admin")
    with patch("app.approval.provisioner.upgrade", return_value=MagicMock(success=True)):
        done = svc.execute_approved(req.request_id)
    assert done.status == EXECUTED
    assert svc.mapping.get("u1").tier == "power"
