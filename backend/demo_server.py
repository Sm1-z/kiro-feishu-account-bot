# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""README 截图用 demo 服务（不入生产链路）。

moto mock 全部 AWS + patch 用量/配额/订阅实况为演示数据，seed 虚构
用户（张三/李四/王五），本地起 uvicorn 供 Playwright 截图。
用法：JWT_SECRET=demo ADMIN_OPEN_IDS=ou_demo_admin python demo_server.py
"""
import os
import sys
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "demo-secret")
os.environ.setdefault("ADMIN_OPEN_IDS", "ou_demo_admin")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("IDENTITY_STORE_ID", "d-demo")

sys.path.insert(0, os.path.dirname(__file__))

import boto3
from moto import mock_aws

mock = mock_aws()
mock.start()

# ---- 建表 ----
from app.config import settings

ddb = boto3.client("dynamodb", region_name=settings.aws_region)
ddb.create_table(
    TableName=settings.mapping_table_name, BillingMode="PAY_PER_REQUEST",
    AttributeDefinitions=[
        {"AttributeName": "kiro_user_id", "AttributeType": "S"},
        {"AttributeName": "feishu_open_id", "AttributeType": "S"},
    ],
    KeySchema=[{"AttributeName": "kiro_user_id", "KeyType": "HASH"}],
    GlobalSecondaryIndexes=[{
        "IndexName": settings.mapping_gsi_name,
        "KeySchema": [{"AttributeName": "feishu_open_id", "KeyType": "HASH"}],
        "Projection": {"ProjectionType": "ALL"},
    }],
)
ddb.create_table(
    TableName=f"{settings.mapping_table_name}-requests", BillingMode="PAY_PER_REQUEST",
    AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
    KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
)

# ---- seed 映射 + 申请记录 ----
from app.mapping_store import AccountMapping, MappingStore
from app.request_store import Request, RequestStore

store = MappingStore()
DEMO_ACCOUNTS = [
    ("u-001", "ou_demo_admin", "张三", "zhangsan", "zhangsan@example.com", "Team-Backend", "pro max", "primary"),
    ("u-002", "ou_demo_admin", "张三", "zhangsan-new1", "zhangsan+2@example.com", "Team-Backend", "pro", "secondary"),
    ("u-003", "ou_lisi", "李四", "lisi", "lisi@example.com", "Team-Frontend", "pro+", "primary"),
    ("u-004", "ou_wangwu", "王五", "wangwu", "wangwu@example.com", "Team-Algo", "power", "primary"),
    ("u-005", "ou_zhaoliu", "赵六", "zhaoliu", "zhaoliu@example.com", "Team-Frontend", "pro", "primary"),
]
for uid, oid, name, uname, email, team, tier, role in DEMO_ACCOUNTS:
    store.put(AccountMapping(
        kiro_user_id=uid, feishu_open_id=oid, feishu_name=name,
        kiro_username=uname, kiro_email=email, team=team, tier=tier,
        status="active", account_role=role))

reqs = RequestStore()
reqs.create(Request(user_open_id="ou_lisi", user_name="李四", type="apply", status="pending",
                    payload={"username": "lisi-new1", "email": "lisi+2@example.com",
                             "group": "Team-Frontend", "tier": "pro"}))
reqs.create(Request(user_open_id="ou_wangwu", user_name="王五", type="upgrade", status="executed",
                    payload={"username": "wangwu", "target_tier": "power"},
                    result={"new_tier": "power"}))
reqs.create(Request(user_open_id="ou_zhaoliu", user_name="赵六", type="apply", status="rejected",
                    payload={"username": "zhaoliu-new1", "email": "z6@example.com",
                             "group": "Team-Algo", "tier": "pro max"},
                    review_comment="请先使用现有账号，用量不足不批副账号"))
reqs.create(Request(user_open_id="ou_demo_admin", user_name="张三", type="overage_cap",
                    status="executed", payload={"from": "400.0", "to": "500"},
                    result={"status": "APPROVED", "request_id": "demo"}))
# 孙七/周八：进入平台已知用户列表（存量导入「绑定给」下拉显示姓名）
reqs.create(Request(user_open_id="ou_sunqi", user_name="孙七", type="upgrade", status="executed",
                    payload={"username": "sunqi", "target_tier": "pro"},
                    result={"new_tier": "pro"}))
reqs.create(Request(user_open_id="ou_zhouba", user_name="周八", type="upgrade", status="executed",
                    payload={"username": "zhouba-new2", "target_tier": "pro"},
                    result={"new_tier": "pro"}))

# ---- patch 外部数据源为演示数据 ----
DEMO_USAGE = {
    "u-001": {"messages": 4210, "conversations": 320, "credits": 862.5, "overage": 0, "last_active": "2026-07-06", "active_days": 41},
    "u-002": {"messages": 102, "conversations": 12, "credits": 35.3, "overage": 0, "last_active": "2026-06-28", "active_days": 6},
    "u-003": {"messages": 2874, "conversations": 201, "credits": 540.8, "overage": 0, "last_active": "2026-07-07", "active_days": 38},
    "u-004": {"messages": 6031, "conversations": 455, "credits": 1204.2, "overage": 120.0, "last_active": "2026-07-07", "active_days": 45},
    "u-005": {"messages": 1523, "conversations": 98, "credits": 210.4, "overage": 0, "last_active": "2026-07-05", "active_days": 22},
}
DEMO_LIVE = {
    "u-001": {"status": "ACTIVE", "tier": "pro max"},
    "u-002": {"status": "ACTIVE", "tier": "pro"},
    "u-003": {"status": "ACTIVE", "tier": "pro+"},
    "u-004": {"status": "ACTIVE", "tier": "power"},
    "u-005": {"status": "PENDING", "tier": "pro"},
}
DEMO_CAP = {"value": 400.0, "quota_name": "Maximum allowed overage per Kiro profile",
            "adjustable": True, "region": "us-east-1",
            "console_url": "https://us-east-1.console.aws.amazon.com/servicequotas/home/services/kiro/quotas/L-75434B0B"}
DEMO_UNLINKED = [
    {"kiro_user_id": "u-090", "kiro_username": "sunqi", "kiro_email": "sunqi@example.com",
     "display_name": "孙七", "suggested_open_id": "ou_sunqi", "suggested_name": "孙七", "confidence": "email"},
    {"kiro_user_id": "u-091", "kiro_username": "zhouba-new2", "kiro_email": "",
     "display_name": "周八", "suggested_open_id": "ou_zhouba", "suggested_name": "周八", "confidence": "pinyin"},
    {"kiro_user_id": "u-092", "kiro_username": "legacy-svc", "kiro_email": "svc@example.com",
     "display_name": "Legacy Service", "suggested_open_id": "", "suggested_name": "", "confidence": ""},
]

import app.usage as usage_mod
import app.quotas as quotas_mod
import app.provisioner as prov_mod

patches = [
    patch.object(usage_mod, "get_usage_by_user", lambda force=False: DEMO_USAGE),
    patch.object(quotas_mod, "get_overage_cap", lambda force=False: DEMO_CAP),
    patch.object(quotas_mod, "get_pending_cap_request", lambda: None),
    patch.object(prov_mod, "live_subscription_map", lambda force=False: DEMO_LIVE),
]
for p in patches:
    p.start()

# 存量导入与分组走 IDC——moto 支持 identitystore，直接造真数据更省事
idc = boto3.client("identitystore", region_name=settings.aws_region)
# moto 的 identitystore 不需要预建 store；直接注入演示组
for g in ["Team-Backend", "Team-Frontend", "Team-Algo"]:
    try:
        idc.create_group(IdentityStoreId=settings.identity_store_id, DisplayName=g)
    except Exception:
        pass

# unlinked 扫描直接 patch（moto 的 list_users 造数麻烦，且建议逻辑已有单测覆盖）
from app import resolver as resolver_mod

class _DemoImport:
    def list_unlinked(self, known_users=None):
        return DEMO_UNLINKED
    def link(self, kiro_user_id, feishu_open_id, feishu_name):
        raise RuntimeError("demo 只读")

patch.object(resolver_mod, "ImportService", _DemoImport).start()

# ---- 起服务 + 打印演示 token ----
from app.auth import issue_token

print("ADMIN_TOKEN=" + issue_token("ou_demo_admin", "张三"), flush=True)
print("USER_TOKEN=" + issue_token("ou_lisi", "李四"), flush=True)

import uvicorn
from app.main import app  # noqa: E402  (main import 放 patch 之后)

uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning")
