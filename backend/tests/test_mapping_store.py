"""映射层测试（moto mock DynamoDB）。

验证 v2 核心范式：UserId 锚点、1:N 一人多账号、主副维度、配额/校验派生。
"""
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.mapping_store import (  # noqa: E402
    AccountMapping,
    MappingStore,
    PRIMARY,
    SECONDARY,
    PERMANENT,
    MONTHLY,
)

from app.config import settings  # noqa: E402

TABLE = "kiro-account-mapping"
GSI = "feishu_open_id-index"
REGION = settings.aws_region  # 与 MappingStore 内部一致，避免 region 不匹配


@pytest.fixture
def store():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "kiro_user_id", "AttributeType": "S"},
                {"AttributeName": "feishu_open_id", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "kiro_user_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": GSI,
                    "KeySchema": [
                        {"AttributeName": "feishu_open_id", "KeyType": "HASH"}
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        yield MappingStore(table_name=TABLE, gsi_name=GSI)


def test_put_and_get_by_userid(store):
    """正查：UserId → 映射，O(1) 命中。"""
    m = AccountMapping(
        kiro_user_id="test-uid-aaaa",
        feishu_open_id="ou_zhangsan",
        feishu_name="张三",
        kiro_username="zhangsan",
        tier="pro+",
        account_role=PRIMARY,
    )
    store.put(m)
    got = store.get("test-uid-aaaa")
    assert got is not None
    assert got.feishu_name == "张三"
    assert got.account_role == PRIMARY
    assert got.retention == PERMANENT  # 由 role 推导


def test_one_person_multiple_accounts(store):
    """1:N — 一个人多个账号，GSI 反查全部。"""
    store.put(AccountMapping(kiro_user_id="test-uid-aaaa", feishu_open_id="ou_zhangsan",
                             kiro_username="zhangsan", account_role=PRIMARY))
    store.put(AccountMapping(kiro_user_id="test-uid-bbbb", feishu_open_id="ou_zhangsan",
                             kiro_username="zhangsan-new1", account_role=SECONDARY))
    store.put(AccountMapping(kiro_user_id="test-uid-cccc", feishu_open_id="ou_lisi",
                             kiro_username="lisi", account_role=PRIMARY))

    zhangsan_accounts = store.list_by_feishu("ou_zhangsan")
    assert len(zhangsan_accounts) == 2
    usernames = {a.kiro_username for a in zhangsan_accounts}
    assert usernames == {"zhangsan", "zhangsan-new1"}

    assert store.count_active("ou_zhangsan") == 2
    assert store.count_active("ou_lisi") == 1


def test_primary_secondary_and_retention(store):
    """主副维度 + retention 自动推导。"""
    store.put(AccountMapping(kiro_user_id="u1", feishu_open_id="ou_x",
                             account_role=PRIMARY))
    store.put(AccountMapping(kiro_user_id="u2", feishu_open_id="ou_x",
                             account_role=SECONDARY))

    assert store.has_primary("ou_x") is True

    secondaries = store.list_secondary()
    assert [s.kiro_user_id for s in secondaries] == ["u2"]
    assert secondaries[0].retention == MONTHLY


def test_update_role_syncs_retention(store):
    """改 account_role 自动同步 retention（副转主）。"""
    store.put(AccountMapping(kiro_user_id="u2", feishu_open_id="ou_x",
                             account_role=SECONDARY))
    store.update_fields("u2", account_role=PRIMARY)
    got = store.get("u2")
    assert got.account_role == PRIMARY
    assert got.retention == PERMANENT


def test_email_dedup_and_username_occupancy(store):
    """email 去重 + username 占用校验（申请前置校验依赖）。"""
    store.put(AccountMapping(kiro_user_id="u1", feishu_open_id="ou_x",
                             kiro_username="zhangsan", kiro_email="a@x.com"))
    assert store.find_by_email("a@x.com").kiro_user_id == "u1"
    assert store.find_by_email("none@x.com") is None
    assert "zhangsan" in store.all_usernames()


def test_delete_mapping_only(store):
    """解除关联：删本地映射，get 返回 None。"""
    store.put(AccountMapping(kiro_user_id="u1", feishu_open_id="ou_x"))
    store.delete("u1")
    assert store.get("u1") is None


def test_cancel_status(store):
    """取消订阅：status=cancelled 后不计入 active。"""
    store.put(AccountMapping(kiro_user_id="u1", feishu_open_id="ou_x",
                             account_role=PRIMARY))
    store.update_fields("u1", status="cancelled")
    assert store.count_active("ou_x") == 0
    assert store.has_primary("ou_x") is False
