"""关联解析器测试。

验证：拼音主名提取（忽略括号别名）、候选生成、新用户名推荐跳过占用、
运行态 Resolver 纯 DB 读、主/副自动判定。
"""
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.mapping_store import MappingStore, PRIMARY, SECONDARY, AccountMapping  # noqa: E402
import app.resolver as R  # noqa: E402

TABLE, GSI, REGION = "kiro-account-mapping", "feishu_open_id-index", settings.aws_region


@pytest.fixture
def store():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "kiro_user_id", "AttributeType": "S"},
                {"AttributeName": "feishu_open_id", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "kiro_user_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{
                "IndexName": GSI,
                "KeySchema": [{"AttributeName": "feishu_open_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        yield MappingStore(table_name=TABLE, gsi_name=GSI)


# ---- 拼音 / 候选（纯函数）----

def test_main_name_pinyin_ignores_alias():
    assert R.main_name_pinyin("张三(Sam)") == "zhangsan"
    assert R.main_name_pinyin("张三（Sam）") == "zhangsan"  # 中文括号
    assert R.main_name_pinyin("张三") == "zhangsan"
    assert R.main_name_pinyin("") == ""


def test_link_candidates_shape():
    c = R.link_candidates("张三")
    assert c[0] == "zhangsan"
    assert "zhangsan5" in c and "zhangsan-new" in c and "zhangsan-new5" in c
    assert len(c) == 1 + 5 + 1 + 5  # base + 1~5 + -new + -new1~5


def test_suggest_new_username_skips_taken():
    taken = {"zhangsan", "zhangsan-new1"}
    assert R.suggest_new_username("张三", taken) == "zhangsan-new2"
    # 全占用则继续递增
    taken |= {f"zhangsan-new{i}" for i in range(1, 6)}
    assert R.suggest_new_username("张三", taken) == "zhangsan-new6"


# ---- 运行态 Resolver（纯 DB，零探测）----

def test_record_new_account_first_is_primary(store):
    r = R.Resolver(store)
    m1 = r.record_new_account("u1", "ou_li", "张三", "zhangsan", "l@x.com", "pro+", "team-a")
    assert m1.account_role == PRIMARY  # 首个=主

    m2 = r.record_new_account("u2", "ou_li", "张三", "zhangsan-new1", "l2@x.com", "power", "team-a")
    assert m2.account_role == SECONDARY  # 第二个=副


def test_accounts_of_is_db_only(store):
    """运行态主路径只读 DB，返回该人全部账号。"""
    r = R.Resolver(store)
    r.record_new_account("u1", "ou_li", "张三", "zhangsan", "l@x.com", "pro", "t")
    r.record_new_account("u2", "ou_li", "张三", "zhangsan-new1", "l2@x.com", "pro", "t")
    accounts = r.accounts_of("ou_li")
    assert len(accounts) == 2
    assert {a.kiro_user_id for a in accounts} == {"u1", "u2"}


def test_manual_link_and_unlink(store):
    r = R.Resolver(store)
    r.manual_link("u9", "ou_zhang", feishu_name="张三", kiro_username="zhangsan")
    assert r.store.get("u9").feishu_open_id == "ou_zhang"
    r.unlink("u9")
    assert r.store.get("u9") is None


def test_resolver_has_no_idc_calls(store):
    """关键不变量：运行态 Resolver 不持有/不调用任何 IDC client。"""
    r = R.Resolver(store)
    # Resolver 实例不应有 _idc / session 属性（那是 MigrationResolver 的）
    assert not hasattr(r, "_idc")
    assert not hasattr(r, "_session")


# ---- username_base（存量导入归属建议）----

def test_username_base_strips_suffixes():
    assert R.username_base("zhangsan") == "zhangsan"
    assert R.username_base("zhangsan2") == "zhangsan"
    assert R.username_base("zhangsan-new") == "zhangsan"
    assert R.username_base("zhangsan-new3") == "zhangsan"
    assert R.username_base("") == ""


# ---- ImportService（发现游离账号 + 归属建议 + 绑定）----

def _make_import_service(store, idc_users):
    """构造 ImportService：mock IDC 分页返回 idc_users。"""
    from unittest.mock import MagicMock, patch

    svc = R.ImportService.__new__(R.ImportService)
    svc.store = store
    svc.resolver = R.Resolver(store)
    svc._id_store = "d-1"
    idc = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Users": idc_users}]
    idc.get_paginator.return_value = paginator
    svc._idc = idc
    return svc, idc


def test_list_unlinked_diffs_and_suggests(store):
    # 映射表已有 uid-linked；IDC 有 3 个用户
    store.put(AccountMapping(kiro_user_id="uid-linked", feishu_open_id="ou_a",
                             feishu_name="张三", kiro_username="zhangsan"))
    idc_users = [
        {"UserId": "uid-linked", "UserName": "zhangsan",
         "Emails": [{"Value": "z@x.com", "Primary": True}]},
        {"UserId": "uid-2", "UserName": "lisi-new2", "DisplayName": "李四",
         "Emails": [{"Value": "lisi@x.com", "Primary": True}]},
        {"UserId": "uid-3", "UserName": "wangwu", "DisplayName": "王五",
         "Emails": [{"Value": "ww@x.com", "Primary": True}]},
    ]
    svc, _ = _make_import_service(store, idc_users)
    known = [
        {"open_id": "ou_b", "name": "李四", "email": ""},          # 拼音命中 lisi-new2
        {"open_id": "ou_c", "name": "赵六", "email": "ww@x.com"},  # 邮箱命中 wangwu
    ]
    out = svc.list_unlinked(known_users=known)
    # 已关联的 uid-linked 被 diff 掉
    ids = [r["kiro_user_id"] for r in out]
    assert "uid-linked" not in ids and set(ids) == {"uid-2", "uid-3"}
    by_id = {r["kiro_user_id"]: r for r in out}
    # 邮箱匹配优先且高可信
    assert by_id["uid-3"]["confidence"] == "email"
    assert by_id["uid-3"]["suggested_open_id"] == "ou_c"
    # 拼音基名匹配（lisi-new2 → lisi → 李四）
    assert by_id["uid-2"]["confidence"] == "pinyin"
    assert by_id["uid-2"]["suggested_open_id"] == "ou_b"


def test_import_link_writes_mapping_with_idc_details(store):
    svc, idc = _make_import_service(store, [])
    idc.describe_user.return_value = {
        "UserName": "lisi-new2",
        "Emails": [{"Value": "lisi@x.com", "Primary": True}],
    }
    m = svc.link("uid-2", "ou_b", "李四")
    assert m.kiro_username == "lisi-new2"
    assert m.kiro_email == "lisi@x.com"
    assert m.account_role == PRIMARY  # 首个账号自动主
    # 落库可反查
    got = store.list_by_feishu("ou_b")
    assert len(got) == 1 and got[0].kiro_user_id == "uid-2"
    assert got[0].status == "active"
