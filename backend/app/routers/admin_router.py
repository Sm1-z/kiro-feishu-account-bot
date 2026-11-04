# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""管理员路由：Web 审批（与飞书卡片审批同源）+ 用户/账号管理。

审批走与 feishu_ws 相同的 ApprovalService（防重 + 异步执行），保证两个入口一致。
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException

from app.approval import ApprovalService
from app.auth import CurrentUser, require_admin
from app.config import settings
from app.mapping_store import MappingStore
from app.request_store import RequestStore
from app.schemas import (
    DeactivateIn, GroupIn, ImportLinkIn, ManualLinkIn, OverageCapIn, ReactivateIn, ReviewIn,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _run_execution(request_id: str, reviewer_name: str):
    svc = ApprovalService()
    req = svc.execute_approved(request_id)
    if req:
        svc.notify_after_execution(req, reviewer_name)


@router.get("/requests")
def list_requests(status: str | None = None, _: CurrentUser = Depends(require_admin)):
    return [r.__dict__ for r in RequestStore().list_by_status(status)]


@router.post("/requests/{request_id}/approve")
def approve(request_id: str, admin: CurrentUser = Depends(require_admin)):
    svc = ApprovalService()
    if not svc.claim_approve(request_id, admin.open_id):
        raise HTTPException(409, "该申请已被处理")
    # 后台执行 AWS 开通（与飞书卡片审批一致）
    threading.Thread(target=_run_execution, args=(request_id, admin.name),
                     daemon=True).start()
    return {"request_id": request_id, "status": "approved", "message": "已受理，正在执行"}


@router.post("/requests/{request_id}/reject")
def reject(request_id: str, body: ReviewIn, admin: CurrentUser = Depends(require_admin)):
    svc = ApprovalService()
    if not svc.claim_reject(request_id, admin.open_id, body.comment):
        raise HTTPException(409, "该申请已被处理")
    req = svc.requests.get(request_id)
    if req:
        svc.notify_after_execution(req, admin.name)  # 通知申请人被拒
    return {"request_id": request_id, "status": "rejected"}


@router.get("/users")
def list_users(_: CurrentUser = Depends(require_admin)):
    """全量映射（飞书用户 → 账号）。前端按 feishu_open_id 聚合展示一人多账号。"""
    store = MappingStore()
    items, kwargs = [], {}
    while True:
        resp = store._table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


@router.get("/accounts")
def list_accounts(force: bool = False, _: CurrentUser = Depends(require_admin)):
    """账号总览：全量映射 + JOIN 订阅实况 + JOIN Kiro 用量（对接 Analytics Dashboard）。

    - 映射表的 status/tier 是平台操作时的快照；管理员在 AWS 控制台直接退订/改套餐
      不会回写映射表，所以每次都拉 ListUserSubscriptions 实况（live_* 字段），
      前端以 live 为准展示、快照留作对照。实况拉取失败时 live_synced=False 降级。
    - 用量关联键 = kiro_user_id = Athena 表 userid，应用层拼接；
      Athena 未配置/失败时 usage 字段为 None（前端显示 —），不阻断账号列表。
    - 费用/额度（billing_* 字段）：套餐、本周期费用、月额度、超额单价直读订阅计费服务（按用户，Kiro Enterprise）；
      **本月已用 credits 与超额 credits 取自 User Activity Report 的本月聚合（活动口径，日级）**——
      计费服务的按用户 currentUsage 对企业用户恒为 0（其按用户查询读的是个人版的聚合键，源码确认），
      原值保留在 billing_zorn_* 字段作对照。超额费用 = 本月超额 credits × 超额单价（预估）。
    - force=true 时绕过用量/费用 TTL 缓存强制重查（刷新按钮用）。
    """
    from app.billing import DEFAULT_OVERAGE_RATE, get_billing_by_user
    from app.provisioner import live_subscription_map
    from app.usage import get_current_month_usage_by_user, get_usage_by_user

    store = MappingStore()
    items, kwargs = [], {}
    while True:
        resp = store._table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    live_synced = True
    try:
        live = live_subscription_map(force=force)  # {user_id: {status, tier}}
    except Exception:
        live, live_synced = {}, False

    usage = get_usage_by_user(force=force)  # {userid: {...}}，失败返回 {}
    user_ids = [it.get("kiro_user_id", "") for it in items]
    billing = get_billing_by_user(user_ids, force=force)  # {userid: {...}}，失败返回 {}
    month_usage = get_current_month_usage_by_user(force=force)  # {userid: {credits, overage, ...}}，失败返回 {}
    month_available = bool(month_usage)
    out = []
    for it in items:
        uid = it.get("kiro_user_id", "")
        u = usage.get(uid)
        lv = live.get(uid)
        b = billing.get(uid)
        mu = month_usage.get(uid)
        # 本月已用 / 超额 credits：报表可用但该用户无记录 = 0；报表不可用 = None
        mtd_credits = mu["credits"] if mu else (0.0 if month_available else None)
        mtd_overage = mu["overage"] if mu else (0.0 if month_available else None)
        rate = (b or {}).get("overage_rate") or DEFAULT_OVERAGE_RATE
        overage_est = round(mtd_overage * rate, 4) if mtd_overage is not None else None
        out.append({
            "kiro_user_id": uid,
            "feishu_open_id": it.get("feishu_open_id", ""),
            "feishu_name": it.get("feishu_name", ""),
            "kiro_username": it.get("kiro_username", ""),
            "kiro_email": it.get("kiro_email", ""),
            "team": it.get("team", ""),
            "tier": it.get("tier", ""),
            "status": it.get("status", ""),
            "account_role": it.get("account_role", ""),
            # 订阅实况（live_synced=False 时不可信，前端回退快照）
            "live_synced": live_synced,
            "live_status": lv["status"] if lv else None,  # None = 无订阅（已退订/未订阅）
            "live_tier": lv["tier"] if lv else None,
            # 用量（None = 无数据/未配置；0 = 有数据但未使用，前端可区分）
            "usage_messages": u["messages"] if u else None,
            "usage_credits": round(u["credits"], 2) if u else None,
            "usage_conversations": u["conversations"] if u else None,
            "usage_last_active": u["last_active"] if u else None,
            "usage_active_days": u["active_days"] if u else None,
            # 费用/额度（计费口径，None = 查询失败或无订阅记录）
            "billing_synced": bool(b and b.get("billing_synced")),
            "billing_error": (b or {}).get("billing_error", "") if b else "unavailable",
            "billing_plan_code": (b or {}).get("plan_code"),
            "billing_plan_start": (b or {}).get("plan_start"),
            "billing_current_charges": (b or {}).get("current_charges"),
            "billing_currency": (b or {}).get("currency", "USD"),
            "billing_account_status": (b or {}).get("account_status"),
            "billing_pending_plan": (b or {}).get("pending_plan"),
            "billing_pending_scheduled": (b or {}).get("pending_scheduled"),
            "billing_credit_limit": (b or {}).get("credit_limit"),
            "billing_overage_rate": (b or {}).get("overage_rate"),
            # 本月活动（User Activity Report 本月聚合）：消息数 / 活跃天数 / 最后活跃；None = 报表不可用
            "usage_month_messages": (mu["messages"] if mu else (0 if month_available else None)),
            "usage_month_active_days": (mu["active_days"] if mu else (0 if month_available else None)),
            "usage_month_last_active": (mu["last_active"] if mu and mu.get("last_active") else None),
            # 本月已用 / 超额：活动口径（User Activity Report 本月聚合），None = 报表不可用
            "billing_credit_used": mtd_credits,
            "billing_credit_used_source": "athena_mtd" if month_available else None,
            "billing_overage_credits": mtd_overage,
            "billing_overage_charges": overage_est,  # 预估 = 超额 credits × 单价
            # 订阅计费服务原值（企业用户恒为 0，仅作对照）
            "billing_zorn_credit_used": (b or {}).get("credit_used"),
            "billing_zorn_overage_charges": (b or {}).get("overage_charges"),
        })
    return out


@router.get("/accounts/{kiro_user_id}/billing-history")
def billing_history(kiro_user_id: str, year: int, month: int,
                    _: CurrentUser = Depends(require_admin)):
    """某账号某月的用量 / 超额（历史回溯，出账对账 / 计费争议用）。

    数据源 = User Activity Report 按自然月聚合（活动口径）。订阅计费服务的按用户历史对企业用户为空，不用。
    月额度与超额单价取当前订阅（计费服务），用于换算超额预估费用。
    available=False 表示报表未配置/不可用；has_usage_entry=False 表示该月无记录。
    """
    from app import usage as usage_mod
    from app.billing import DEFAULT_OVERAGE_RATE, get_billing_by_user

    if not (2024 <= year <= 2100) or not (1 <= month <= 12):
        raise HTTPException(400, "year/month 不合法")
    available = usage_mod._enabled()
    data = usage_mod.get_month_usage_by_user(year, month) if available else {}
    mu = data.get(kiro_user_id)
    b = (get_billing_by_user([kiro_user_id]) or {}).get(kiro_user_id) or {}
    rate = b.get("overage_rate") or DEFAULT_OVERAGE_RATE
    out = {
        "user_id": kiro_user_id, "year": year, "month": month,
        "source": "athena" if available else None, "available": available,
        "has_usage_entry": bool(mu),
        "credit_used": mu["credits"] if mu else None,
        "overage_credits": mu["overage"] if mu else None,
        "overage_rate": rate,
        "overage_charges_est": round(mu["overage"] * rate, 4) if mu else None,
        "messages": mu["messages"] if mu else None,
        "active_days": mu["active_days"] if mu else None,
        "last_active": mu["last_active"] if mu else None,
        "credit_limit": b.get("credit_limit"),   # 当前套餐额度，仅供参照
        "plan_code": b.get("plan_code"),
    }
    return out


@router.get("/overage-cap")
def overage_cap(_: CurrentUser = Depends(require_admin)):
    """Overages 超额上限（Service Quotas，USD/订阅）。查询失败返回 cap=None（前端显示 —）。"""
    from app.quotas import get_overage_cap, get_pending_cap_request

    cap = get_overage_cap()
    pending = None
    if cap is not None:
        try:
            pending = get_pending_cap_request()
        except Exception:
            pending = None  # pending 查询失败不影响 cap 展示
    return {"cap": cap, "pending": pending}


@router.post("/overage-cap")
def raise_overage_cap(body: OverageCapIn, admin: CurrentUser = Depends(require_admin)):
    """调高 Overages 上限（increase-only）。即时执行，落 requests 表做审计。"""
    from app import request_store as RS
    from app.quotas import get_overage_cap, request_cap_increase

    before = get_overage_cap()
    try:
        result = request_cap_increase(body.desired_value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # 审计：谁、何时、从多少调到多少。执行已完成，直接落 executed。
    RS.RequestStore().create(RS.Request(
        user_open_id=admin.open_id, user_name=admin.name, type=RS.OVERAGE_CAP,
        status=RS.EXECUTED, reviewer_open_id=admin.open_id,
        payload={"from": str(before["value"] if before else "?"),
                 "to": str(body.desired_value)},
        result={"status": result["status"], "request_id": result["request_id"]},
    ))
    return result


@router.post("/accounts/deactivate")
def deactivate_accounts(body: DeactivateIn, admin: CurrentUser = Depends(require_admin)):
    """停用 / 回收账号（即时执行，管理员动作，逐账号落 requests 表审计）。

    对应 Kiro 控制台「Deactivate Kiro plan」：
    - mode=end_of_period（默认）：访问与计费都到月底结束，期间订阅实况为 CANCELLED；
    - mode=immediate：立即切断访问，**当月仍整月计费**（不省钱），适合离职/安全事件。
    recycle=True 时额外删 IDC 用户 + 解除映射（副账号回收，不可逆）。
    返回逐账号结果；部分失败时整体 HTTP 200，由前端按行展示 error。
    """
    from app import request_store as RS
    from app.provisioner import bulk_cancel, deactivate

    store = MappingStore()
    name_of = {}
    for uid in body.kiro_user_ids:
        m = store.get(uid)
        name_of[uid] = m.kiro_username if m else ""

    if body.recycle:
        results = bulk_cancel(body.kiro_user_ids, body.mode)
        rtype = RS.RECYCLE
    else:
        results = deactivate(body.kiro_user_ids, body.mode)
        rtype = RS.DEACTIVATE

    # 审计：谁、何时、对哪个账号、哪种方式、结果。执行已完成，直接落 executed/failed。
    rs = RS.RequestStore()
    for r in results:
        rs.create(RS.Request(
            user_open_id=admin.open_id, user_name=admin.name, type=rtype,
            status=RS.EXECUTED if r["success"] else RS.FAILED,
            reviewer_open_id=admin.open_id, kiro_user_id=r["user_id"],
            payload={"mode": body.mode, "username": name_of.get(r["user_id"], "")},
            result={"error": r["error"]} if r["error"] else {"ok": True},
        ))
    for r in results:
        r["username"] = name_of.get(r["user_id"], "")
    return {"mode": body.mode, "recycle": body.recycle, "results": results,
            "succeeded": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"])}


@router.post("/accounts/reactivate")
def reactivate_accounts(body: ReactivateIn, admin: CurrentUser = Depends(require_admin)):
    """恢复订阅（撤销月底停用）：对 CANCELLED 账号按原套餐重新订阅，逐账号审计。

    对应控制台停用成功提示里的 "You can cancel this removal by resubscribing them
    before the end of the month"。实测两种停用方式后同月都可重订，订阅回到 PENDING。
    """
    from app import request_store as RS
    from app.provisioner import reactivate

    store = MappingStore()
    name_of = {uid: (store.get(uid).kiro_username if store.get(uid) else "") for uid in body.kiro_user_ids}
    results = reactivate(body.kiro_user_ids)
    rs = RS.RequestStore()
    for r in results:
        rs.create(RS.Request(
            user_open_id=admin.open_id, user_name=admin.name, type=RS.REACTIVATE,
            status=RS.EXECUTED if r["success"] else RS.FAILED,
            reviewer_open_id=admin.open_id, kiro_user_id=r["user_id"],
            payload={"username": name_of.get(r["user_id"], ""), "tier": r.get("tier", "")},
            result={"error": r["error"]} if r["error"] else {"ok": True},
        ))
        r["username"] = name_of.get(r["user_id"], "")
    return {"results": results,
            "succeeded": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"])}


@router.post("/groups")
def create_group(body: GroupIn, _: CurrentUser = Depends(require_admin)):
    """新建分组 = 在 IAM Identity Center 建 Group（申请表单的分组下拉即 IDC 组）。"""
    from botocore.exceptions import ClientError

    from app.aws import get_identity_store_id, get_session

    name = body.group_name.strip()
    session = get_session()
    id_store = get_identity_store_id(session)
    idc = session.client("identitystore", region_name=settings.aws_region)
    try:
        resp = idc.create_group(IdentityStoreId=id_store, DisplayName=name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConflictException":
            raise HTTPException(400, f"分组 '{name}' 已存在")
        raise HTTPException(500, f"创建分组失败: {exc.response['Error']['Message']}")
    return {"group_id": resp["GroupId"], "group_name": name}


def _known_feishu_users() -> list[dict]:
    """平台已知的飞书用户 = 映射表 + 申请记录去重（open_id → name）。

    平台不落库飞书邮箱（OAuth 拿到但只进 JWT），故 email 建议匹配仅在
    映射表 kiro_email 命中时生效；拼音建议靠 name。
    """
    users: dict[str, dict] = {}
    for m in MappingStore().list_all():
        if m.feishu_open_id and m.feishu_open_id not in users:
            users[m.feishu_open_id] = {
                "open_id": m.feishu_open_id, "name": m.feishu_name,
                "email": m.kiro_email,
            }
    for r in RequestStore().list_by_status():
        if r.user_open_id and r.user_open_id not in users:
            users[r.user_open_id] = {
                "open_id": r.user_open_id, "name": r.user_name, "email": "",
            }
    return list(users.values())


@router.get("/unlinked")
def list_unlinked(_: CurrentUser = Depends(require_admin)):
    """存量导入①：IDC 游离账号（未在映射表中）+ 归属建议。管理员点按钮触发。"""
    from app.resolver import ImportService

    return ImportService().list_unlinked(known_users=_known_feishu_users())


@router.get("/feishu-users")
def feishu_users(_: CurrentUser = Depends(require_admin)):
    """存量导入②：可选绑定目标（平台已知的飞书用户），手工绑定下拉用。"""
    return _known_feishu_users()


@router.post("/unlinked/link")
def link_unlinked(body: ImportLinkIn, _: CurrentUser = Depends(require_admin)):
    """存量导入③：确认绑定。用户名/邮箱从 IDC 自动补全，主/副自动判定。"""
    from app.resolver import ImportService

    try:
        m = ImportService().link(
            kiro_user_id=body.kiro_user_id,
            feishu_open_id=body.feishu_open_id,
            feishu_name=body.feishu_name,
        )
    except Exception as exc:
        raise HTTPException(400, f"绑定失败: {exc}")
    return m.__dict__


@router.post("/link")
def manual_link(body: ManualLinkIn, _: CurrentUser = Depends(require_admin)):
    from app.resolver import Resolver
    m = Resolver().manual_link(
        kiro_user_id=body.kiro_user_id, feishu_open_id=body.feishu_open_id,
        feishu_name=body.feishu_name, kiro_username=body.kiro_username,
        kiro_email=body.kiro_email, tier=body.tier,
    )
    return m.__dict__


@router.delete("/mappings/{kiro_user_id}")
def unlink(kiro_user_id: str, _: CurrentUser = Depends(require_admin)):
    MappingStore().delete(kiro_user_id)
    return {"deleted": kiro_user_id}