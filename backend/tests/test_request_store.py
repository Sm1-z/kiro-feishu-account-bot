"""审批流存储测试 —— 重点验证状态机防重（条件更新）。"""
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.request_store import (  # noqa: E402
    APPLY, APPROVED, EXECUTED, PENDING, REJECTED,
    Request, RequestStore,
)

REGION = settings.aws_region
TABLE = f"{settings.mapping_table_name}-requests"


@pytest.fixture
def store():
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
        )
        yield RequestStore(table_name=TABLE)


def _mk(store, **kw):
    r = Request(user_open_id=kw.get("user_open_id", "ou_a"), type=APPLY,
                payload=kw.get("payload", {"username": "zhangsan", "tier": "pro+"}))
    return store.create(r)


def test_create_and_get(store):
    r = _mk(store)
    got = store.get(r.request_id)
    assert got.status == PENDING
    assert got.payload["username"] == "zhangsan"


def test_transition_dedup(store):
    """核心：pending→approved 只能成功一次（防多管理员重复执行）。"""
    r = _mk(store)
    first = store.transition(r.request_id, PENDING, APPROVED, reviewer_open_id="ou_admin")
    second = store.transition(r.request_id, PENDING, APPROVED, reviewer_open_id="ou_admin2")
    assert first is True
    assert second is False  # 已不是 pending，条件失败
    assert store.get(r.request_id).status == APPROVED
    assert store.get(r.request_id).reviewer_open_id == "ou_admin"


def test_transition_approve_then_execute(store):
    r = _mk(store)
    assert store.transition(r.request_id, PENDING, APPROVED) is True
    assert store.transition(r.request_id, APPROVED, EXECUTED,
                            result={"user_id": "test-uid-x"}) is True
    assert store.get(r.request_id).status == EXECUTED


def test_reject_blocks_later_approve(store):
    r = _mk(store)
    assert store.transition(r.request_id, PENDING, REJECTED) is True
    assert store.transition(r.request_id, PENDING, APPROVED) is False


def test_list_by_status_and_user(store):
    _mk(store, user_open_id="ou_a")
    r2 = _mk(store, user_open_id="ou_b")
    store.transition(r2.request_id, PENDING, APPROVED)

    pending = store.list_by_status(PENDING)
    assert len(pending) == 1 and pending[0].user_open_id == "ou_a"

    mine = store.list_by_user("ou_b")
    assert len(mine) == 1 and mine[0].status == APPROVED
