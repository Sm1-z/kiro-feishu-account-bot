# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""README 截图用 demo 服务（不入生产链路）。

moto mock 全部 AWS + patch 用量 / 积分池 / 配额为演示数据（预付池，席位已满），
seed 虚构用户（张三/李四/王五/赵六），本地起 uvicorn（仅 127.0.0.1）供 Playwright 截图。
写操作（停用 / 设上限 / 审批开通）全部 patch 成不连 AWS 的假成功。
用法：ADMIN_OPEN_IDS=ou_demo_admin python demo_server.py
"""
import os
import sys
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "demo-secret-only-for-local-screenshots-0123")
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
import datetime as _dt

from app.mapping_store import AccountMapping, MappingStore
from app.request_store import Request, RequestStore

store = MappingStore()
# (uid, open_id, 姓名, 用户名, 邮箱, 分组, 个人上限快照, 角色；每人一个账号)
DEMO_ACCOUNTS = [
    ("u-001", "ou_demo_admin", "张三", "zhangsan", "zhangsan@example.com", "Team-Backend", 1500, "primary"),
    ("u-002", "ou_qianqi", "钱七", "qianqi", "qianqi@example.com", "Team-Backend", None, "primary"),
    ("u-003", "ou_lisi", "李四", "lisi", "lisi@example.com", "Team-Frontend", None, "primary"),
    ("u-004", "ou_wangwu", "王五", "wangwu", "wangwu@example.com", "Team-Algo", 800, "primary"),
    ("u-005", "ou_zhaoliu", "赵六", "zhaoliu", "zhaoliu@example.com", "Team-Frontend", None, "primary"),
]
for uid, oid, name, uname, email, team, limit, role in DEMO_ACCOUNTS:
    store.put(AccountMapping(
        kiro_user_id=uid, feishu_open_id=oid, feishu_name=name,
        kiro_username=uname, kiro_email=email, team=team, usage_limit=limit,
        status="active", account_role=role))

reqs = RequestStore()
reqs.create(Request(user_open_id="ou_zhouba", user_name="周八", type="apply", status="pending",
                    payload={"username": "zhouba", "email": "zhouba@example.com",
                             "group": "Team-Frontend", "usage_limit": None,
                             "reason": "新入职，负责前端组件库迁移"}))
reqs.create(Request(user_open_id="ou_wangwu", user_name="王五", type="limit_change", status="pending",
                    kiro_user_id="u-004",
                    payload={"username": "wangwu", "requested_limit": 1500, "current_limit": 800,
                             "reason": "训练脚本重构，本月已用到 80%"}))
reqs.create(Request(user_open_id="ou_sunqi", user_name="孙七", type="apply", status="rejected",
                    payload={"username": "sunqi-new", "email": "sunqi+2@example.com",
                             "group": "Team-Algo", "usage_limit": 3000},
                    review_comment="IDC 里已有你的存量账号，管理员会直接绑定给你"))
reqs.create(Request(user_open_id="ou_demo_admin", user_name="张三", type="limit_set",
                    status="executed", payload={"scope": "POOL", "username": "", "from": "None", "to": "600"},
                    result={"ok": True}))
# 孙七/周八：进入平台已知用户列表（存量导入「绑定给」下拉显示姓名）
reqs.create(Request(user_open_id="ou_sunqi", user_name="孙七", type="limit_change", status="executed",
                    payload={"username": "sunqi", "requested_limit": 800}, result={"new_limit": 800}))
reqs.create(Request(user_open_id="ou_zhouba", user_name="周八", type="limit_change", status="executed",
                    payload={"username": "zhouba-new2", "requested_limit": 500}, result={"new_limit": 500}))

# ---- patch 外部数据源为演示数据（预付池，4 个席位全部占满，赵六等待席位）----
DEMO_USAGE = {
    "u-001": {"messages": 4210, "conversations": 320, "credits": 2862.5, "overage": 0, "last_active": "2026-11-26", "active_days": 41},
    "u-002": {"messages": 102, "conversations": 12, "credits": 35.3, "overage": 0, "last_active": "2026-11-12", "active_days": 6},
    "u-003": {"messages": 2874, "conversations": 201, "credits": 1540.8, "overage": 0, "last_active": "2026-11-26", "active_days": 38},
    "u-004": {"messages": 6031, "conversations": 455, "credits": 3204.2, "overage": 0, "last_active": "2026-11-26", "active_days": 45},
}
DEMO_MONTH_USAGE = {
    "u-001": {"credits": 820.0, "overage": 0.0, "messages": 1980, "active_days": 18, "last_active": "2026-11-26"},
    "u-002": {"credits": 35.0, "overage": 0.0, "messages": 102, "active_days": 2, "last_active": "2026-11-12"},
    "u-003": {"credits": 410.0, "overage": 0.0, "messages": 974, "active_days": 15, "last_active": "2026-11-26"},
    "u-004": {"credits": 640.0, "overage": 0.0, "messages": 1531, "active_days": 20, "last_active": "2026-11-26"},
}
DEMO_LIVE = {
    "u-001": {"status": "SEATED", "direct": True, "activated_at": None},
    "u-002": {"status": "SEATED", "direct": True, "activated_at": None},
    "u-003": {"status": "SEATED", "direct": True, "activated_at": None},
    "u-004": {"status": "SEATED", "direct": True, "activated_at": None},
    "u-005": {"status": "AWAITING_SEAT", "direct": True, "activated_at": None},
}
DEMO_EFFECTIVE = {
    "u-001": {"limit": 1500.0, "scope": "USER", "scope_id": "u-001"},
    "u-002": {"limit": 600.0, "scope": "POOL", "scope_id": "111122223333"},
    "u-003": {"limit": 600.0, "scope": "POOL", "scope_id": "111122223333"},
    "u-004": {"limit": 800.0, "scope": "USER", "scope_id": "u-004"},
    "u-005": {"limit": 600.0, "scope": "POOL", "scope_id": "111122223333"},
}
DEMO_LIMITS = [
    {"scope": "POOL", "scope_id": "111122223333", "limit": 600.0, "effective_from": None},
    {"scope": "USER", "scope_id": "u-001", "limit": 1500.0, "effective_from": None},
    {"scope": "USER", "scope_id": "u-004", "limit": 800.0, "effective_from": None},
]
DEMO_UNLINKED = [
    {"kiro_user_id": "u-090", "kiro_username": "sunqi", "kiro_email": "sunqi@example.com",
     "display_name": "孙七", "suggested_open_id": "ou_sunqi", "suggested_name": "孙七", "confidence": "email"},
    {"kiro_user_id": "u-091", "kiro_username": "zhouba-new2", "kiro_email": "",
     "display_name": "周八", "suggested_open_id": "ou_zhouba", "suggested_name": "周八", "confidence": "pinyin"},
    {"kiro_user_id": "u-092", "kiro_username": "legacy-svc", "kiro_email": "svc@example.com",
     "display_name": "Legacy Service", "suggested_open_id": "", "suggested_name": "", "confidence": ""},
]

import app.pool as pool_mod
import app.provisioner as prov_mod
import app.quotas as quotas_mod
import app.usage as usage_mod
from app.routers import request_router as req_router_mod

DEMO_CTX = pool_mod.PoolContext("111122223333", settings.aws_region, "arn:aws:sso:::instance/ssoins-demo",
                                "arn:aws:sso::111122223333:application/ssoins-demo/apl-demo", "PREPAID")
_now = _dt.datetime.now(_dt.timezone.utc)
_cycle_start = _now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
DEMO_ENTITLEMENTS = [
    {"effectiveAt": _cycle_start.timestamp(), "entitlementType": "POOL_SUBSCRIPTION",
     "details": {"poolSubscription": {"quotaPerSeat": 500.0, "seatCount": 4,
                                      "usage": {"quantity": 2000.0, "currentUsage": 1905.0}}}},
    {"entitlementId": "demo-addon-1", "effectiveAt": (_now - _dt.timedelta(days=17)).timestamp(),
     "expiresAt": (_now + _dt.timedelta(days=348)).timestamp(), "entitlementType": "ADDON",
     "details": {"addon": {"source": "MANUAL", "usage": {"quantity": 1000.0, "currentUsage": 0.0}}}},
]
DEMO_SEATS = {"users": DEMO_LIVE, "seated": 4, "awaiting": 1, "total": 4, "available": 0,
              "pending_removal": None, "pending_removal_effective": None}
DEMO_PURCHASES = [{"id": "demo-addon-1", "quantity": 1000, "status": "ACTIVE", "source": "MANUAL",
                   "created_at": (_now - _dt.timedelta(days=17)).isoformat(),
                   "expires_at": (_now + _dt.timedelta(days=348)).isoformat()}]
DEMO_QUOTAS = {
    "onboarding_cap": {"code": "L-295A3FFB", "name": "Maximum allowed users to onboard per Kiro profile",
                       "value": 1000.0, "adjustable": True, "console_url": quotas_mod.console_url("L-295A3FFB")},
    "monthly_purchase_cap": {"code": "L-05AF9163", "name": "Maximum allowed credits to purchase in month",
                             "value": 2500000.0, "adjustable": True, "console_url": quotas_mod.console_url("L-05AF9163")},
}


def _demo_month_usage_by_user(year, month, force=False):
    if month % 2 == 0:  # 偶数月演示「无记录」
        return {}
    return {u: {**v, "credits": round(v["credits"] * 0.8, 1)} for u, v in DEMO_MONTH_USAGE.items()}


def _ok(ids, **extra):
    return [{"user_id": u, "success": True, "error": "", **extra} for u in ids]


patches = [
    patch.object(usage_mod, "get_usage_by_user", lambda force=False: DEMO_USAGE),
    patch.object(usage_mod, "_enabled", lambda: True),
    patch.object(usage_mod, "get_current_month_usage_by_user", lambda force=False: DEMO_MONTH_USAGE),
    patch.object(usage_mod, "get_month_usage_by_user", _demo_month_usage_by_user),
    patch.object(prov_mod, "live_status_map", lambda force=False: DEMO_LIVE),
    patch.object(prov_mod, "deactivate", lambda ids: _ok(ids)),
    patch.object(prov_mod, "bulk_cancel", lambda ids: _ok(ids)),
    patch.object(prov_mod, "reactivate", lambda ids: _ok(ids, warning="")),
    patch.object(prov_mod, "retry_seats", lambda ids: [{"user_id": u, "success": False,
        "error": pool_mod.SEAT_REASON_TEXT["CAPACITY_EXHAUSTED"]} for u in ids]),
    patch.object(prov_mod, "set_user_limit", lambda uid, limit: prov_mod.SimpleResult(success=True)),
    patch.object(prov_mod, "provision", lambda **kw: prov_mod.ProvisionResult(
        success=True, user_id="u-new", warning=pool_mod.SEAT_REASON_TEXT["CAPACITY_EXHAUSTED"],
        steps_succeeded=["user_created", "group_added", "password_email_sent", "pool_joined", "awaiting_seat"])),
    patch.object(pool_mod, "get_context", lambda force=False: DEMO_CTX),
    patch.object(pool_mod, "overview", lambda force=False, now=None: pool_mod.summarize(
        DEMO_ENTITLEMENTS, DEMO_CTX, now or _dt.datetime.now(_dt.timezone.utc))),
    patch.object(pool_mod, "seat_status", lambda force=False: DEMO_SEATS),
    patch.object(pool_mod, "list_limits", lambda force=False: DEMO_LIMITS),
    patch.object(pool_mod, "profile_limit", lambda force=False: 600.0),
    patch.object(pool_mod, "effective_limit", lambda uid: DEMO_EFFECTIVE.get(uid)),
    patch.object(pool_mod, "effective_limits", lambda ids, force=False: {u: DEMO_EFFECTIVE[u] for u in ids if u in DEMO_EFFECTIVE}),
    patch.object(pool_mod, "set_limit", lambda scope, scope_id, limit: {}),
    patch.object(pool_mod, "list_credit_purchases", lambda force=False: DEMO_PURCHASES),
    patch.object(quotas_mod, "get_kiro_quotas", lambda force=False: DEMO_QUOTAS),
    patch.object(req_router_mod, "_pool_hint", lambda req_type: "积分池剩余 1095 / 3000 credits · 已占座 4 人 · 预付：没有空余席位，通过后需先在控制台买席位"),
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
print("NEW_USER_TOKEN=" + issue_token("ou_wujiu", "吴九"), flush=True)  # 还没有账号的员工（截申请弹窗用）

import uvicorn
from app.main import app  # noqa: E402  (main import 放 patch 之后)

uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning")
