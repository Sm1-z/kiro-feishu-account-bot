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
    DeactivateIn, GroupIn, ImportLinkIn, LimitSetIn, ManualLinkIn, ReactivateIn, ReviewIn,
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
    return _scan_mappings()


def _scan_mappings() -> list[dict]:
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
    """账号总览：全量映射 + JOIN 积分池实况 + JOIN 生效上限 + JOIN Kiro 用量。

    - 映射表的 status / usage_limit 是平台操作时的快照；管理员在控制台直接移出 / 改上限不会回写，
      所以每次都拉池实况（pool_status：SEATED 已占座 / AWAITING_SEAT 等待席位 / None 不在池里）。
      实况拉取失败时 live_synced=False，前端回退快照。
    - 生效上限（limit / limit_scope）按人查 GetEffectiveUsageLimit：limit_scope 为 USER / GROUP / POOL，
      NONE = 无上限，None = 查询失败。
    - 用量来自 User Activity Report（Athena）：累计 usage_* 与本月 month_*；
      报表未配置 / 失败时为 None（前端显示 —），不阻断账号列表。
    - force=true 绕过缓存强制重查（刷新按钮用）。
    """
    from app import pool
    from app.provisioner import live_status_map
    from app.usage import get_current_month_usage_by_user, get_usage_by_user

    items = _scan_mappings()
    live_synced = True
    try:
        live = live_status_map(force=force)
    except Exception:
        live, live_synced = {}, False
    user_ids = [it.get("kiro_user_id", "") for it in items]
    try:
        limits = pool.effective_limits(user_ids, force=force)
    except Exception:
        limits = {}
    usage = get_usage_by_user(force=force)
    month_usage = get_current_month_usage_by_user(force=force)
    month_available = bool(month_usage)
    out = []
    for it in items:
        uid = it.get("kiro_user_id", "")
        u, lv, mu = usage.get(uid), live.get(uid), month_usage.get(uid)
        if uid in limits:
            eff = limits[uid]
            limit, scope = (eff["limit"], eff["scope"]) if eff else (None, "NONE")
        else:
            limit, scope = None, None
        month_credits = mu["credits"] if mu else (0.0 if month_available else None)
        snap_limit = it.get("usage_limit")
        out.append({
            "kiro_user_id": uid,
            "feishu_open_id": it.get("feishu_open_id", ""),
            "feishu_name": it.get("feishu_name", ""),
            "kiro_username": it.get("kiro_username", ""),
            "kiro_email": it.get("kiro_email", ""),
            "team": it.get("team", ""),
            "status": it.get("status", ""),
            "account_role": it.get("account_role", ""),
            "usage_limit": int(snap_limit) if snap_limit is not None else None,
            # 积分池实况（live_synced=False 时不可信，前端回退快照）
            "live_synced": live_synced,
            "pool_status": lv["status"] if lv else None,
            "pool_direct": lv["direct"] if lv else None,
            # 生效上限
            "limit": limit,
            "limit_scope": scope,
            "limit_used_pct": (round(month_credits / limit * 100, 1)
                               if month_credits is not None and limit else None),
            # 用量（None = 无数据 / 未配置；0 = 有数据但未使用）
            "usage_messages": u["messages"] if u else None,
            "usage_credits": round(u["credits"], 2) if u else None,
            "usage_last_active": u["last_active"] if u else None,
            "usage_active_days": u["active_days"] if u else None,
            "month_credits": round(month_credits, 2) if month_credits is not None else None,
            "month_messages": mu["messages"] if mu else (0 if month_available else None),
            "month_active_days": mu["active_days"] if mu else (0 if month_available else None),
            "month_last_active": mu["last_active"] if mu and mu.get("last_active") else None,
        })
    return out


@router.get("/accounts/{kiro_user_id}/usage-history")
def usage_history(kiro_user_id: str, year: int, month: int,
                  _: CurrentUser = Depends(require_admin)):
    """某账号某月的用量（User Activity Report 按 UTC 自然月聚合），用于月度复盘 / 对账。

    available=False 表示报表未配置；has_usage_entry=False 表示该月无记录。
    """
    from app import usage as usage_mod

    if not (2024 <= year <= 2100) or not (1 <= month <= 12):
        raise HTTPException(400, "year/month 不合法")
    available = usage_mod._enabled()
    data = usage_mod.get_month_usage_by_user(year, month) if available else {}
    mu = data.get(kiro_user_id)
    return {
        "user_id": kiro_user_id, "year": year, "month": month,
        "source": "athena" if available else None, "available": available,
        "has_usage_entry": bool(mu),
        "credit_used": mu["credits"] if mu else None,
        "messages": mu["messages"] if mu else None,
        "active_days": mu["active_days"] if mu else None,
        "last_active": mu["last_active"] if mu else None,
    }


@router.get("/pool")
def pool_overview(force: bool = False, _: CurrentUser = Depends(require_admin)):
    """积分池总览：余额 / 消耗速度 / 预计耗尽 + 席位 + profile 默认上限 + 购买记录 + 配额。

    各部分独立降级：某项失败为 None 并记到 errors，不影响其余部分。
    """
    from app import pool, quotas

    out: dict = {"errors": [], "console_url": settings.kiro_console_url}
    try:
        ctx = pool.get_context(force=force)
        out["context"] = {"payment_model": ctx.payment_model, "prepaid": ctx.prepaid,
                          "region": ctx.region, "account_id": ctx.account_id}
    except Exception as exc:  # noqa: BLE001 - 上下文拿不到其余都无从谈起
        out["context"] = None
        out["errors"].append(f"context: {exc}")
        return out
    for key, fn in (("balance", lambda: pool.overview(force=force)),
                    ("seats", lambda: {k: v for k, v in pool.seat_status(force=force).items()
                                       if k != "users"}),
                    ("profile_limit", lambda: pool.profile_limit(force=force)),
                    ("purchases", lambda: pool.list_credit_purchases(force=force)),
                    ("quotas", lambda: quotas.get_kiro_quotas(force=force))):
        try:
            out[key] = fn()
        except Exception as exc:  # noqa: BLE001
            out[key] = None
            out["errors"].append(f"{key}: {exc}")
    return out


@router.get("/limits")
def list_limits(force: bool = False, _: CurrentUser = Depends(require_admin)):
    """已设置的全部上限（USER / GROUP / POOL）。"""
    from app import pool

    try:
        return pool.list_limits(force=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"查询上限失败: {exc}")


@router.post("/limits")
def set_limit(body: LimitSetIn, admin: CurrentUser = Depends(require_admin)):
    """管理员直接设置 / 删除上限（即时生效，落 requests 表审计）。

    scope=USER：某账号的个人上限；scope=POOL：profile 默认上限（每人每月）。
    usage_limit=None 表示删除；0 表示禁止使用。
    """
    from app import pool
    from app import request_store as RS

    if body.scope == "USER" and not body.scope_id:
        raise HTTPException(400, "设置个人上限需要 scope_id（账号 UserId）")
    store = MappingStore()
    acct = store.get(body.scope_id) if body.scope == "USER" else None
    try:
        before = (pool.effective_limit(body.scope_id) if body.scope == "USER"
                  else pool.profile_limit(force=True))
    except Exception:  # noqa: BLE001 - before 只用于审计
        before = "?"
    try:
        pool.set_limit(body.scope, body.scope_id, body.usage_limit)
    except pool.PoolError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"设置上限失败: {exc}")
    if acct:
        try:
            store.update_fields(body.scope_id, usage_limit=body.usage_limit)
        except Exception:  # noqa: BLE001
            pass
    RS.RequestStore().create(RS.Request(
        user_open_id=admin.open_id, user_name=admin.name, type=RS.LIMIT_SET,
        status=RS.EXECUTED, reviewer_open_id=admin.open_id,
        kiro_user_id=body.scope_id if body.scope == "USER" else "",
        payload={"scope": body.scope, "username": acct.kiro_username if acct else "",
                 "from": str(before), "to": "删除" if body.usage_limit is None else str(body.usage_limit)},
        result={"ok": True},
    ))
    return {"scope": body.scope, "scope_id": body.scope_id, "usage_limit": body.usage_limit}


def _audit_batch(admin: CurrentUser, rtype: str, results: list[dict], name_of: dict,
                 extra: dict | None = None) -> None:
    from app import request_store as RS

    rs = RS.RequestStore()
    for r in results:
        rs.create(RS.Request(
            user_open_id=admin.open_id, user_name=admin.name, type=rtype,
            status=RS.EXECUTED if r["success"] else RS.FAILED,
            reviewer_open_id=admin.open_id, kiro_user_id=r["user_id"],
            payload={"username": name_of.get(r["user_id"], ""), **(extra or {})},
            result={"error": r["error"]} if r.get("error") else
                   {"ok": True, **({"warning": r["warning"]} if r.get("warning") else {})},
        ))
        r["username"] = name_of.get(r["user_id"], "")


def _names(user_ids: list[str]) -> dict:
    store = MappingStore()
    out = {}
    for uid in user_ids:
        m = store.get(uid)
        out[uid] = m.kiro_username if m else ""
    return out


@router.post("/accounts/deactivate")
def deactivate_accounts(body: DeactivateIn, admin: CurrentUser = Depends(require_admin)):
    """停用 = 移出积分池（即时生效，保留 IDC 用户与映射）；recycle=True 时另删 IDC 用户 + 解除映射。

    已用的 credits 不退，本账期席位费照收，下个账期起不再计费。逐账号落 requests 表审计；
    部分失败时整体 HTTP 200，由前端按行展示 error。
    """
    from app import request_store as RS
    from app.provisioner import bulk_cancel, deactivate

    name_of = _names(body.kiro_user_ids)
    if body.recycle:
        results, rtype = bulk_cancel(body.kiro_user_ids), RS.RECYCLE
    else:
        results, rtype = deactivate(body.kiro_user_ids), RS.DEACTIVATE
    _audit_batch(admin, rtype, results, name_of)
    return {"recycle": body.recycle, "results": results,
            "succeeded": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"])}


@router.post("/accounts/reactivate")
def reactivate_accounts(body: ReactivateIn, admin: CurrentUser = Depends(require_admin)):
    """恢复 = 重新加入积分池（预付池同时分配席位，席位不足时成功但带 warning）。"""
    from app import request_store as RS
    from app.provisioner import reactivate

    name_of = _names(body.kiro_user_ids)
    results = reactivate(body.kiro_user_ids)
    _audit_batch(admin, RS.REACTIVATE, results, name_of)
    return {"results": results,
            "succeeded": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"])}


@router.post("/accounts/retry-seats")
def retry_seats(body: ReactivateIn, admin: CurrentUser = Depends(require_admin)):
    """预付池：对「等待席位」的账号重新分配席位（在控制台买完席位后用）。"""
    from app import request_store as RS
    from app.provisioner import retry_seats as _retry

    name_of = _names(body.kiro_user_ids)
    results = _retry(body.kiro_user_ids)
    _audit_batch(admin, RS.SEAT_RETRY, results, name_of)
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
                "email": m.kiro_email, "has_account": True,  # 每人一个账号：已有账号的不能再绑
            }
    for r in RequestStore().list_by_status():
        if r.user_open_id and r.user_open_id not in users:
            users[r.user_open_id] = {
                "open_id": r.user_open_id, "name": r.user_name, "email": "",
                "has_account": False,
            }
    return list(users.values())


@router.get("/unlinked")
def list_unlinked(_: CurrentUser = Depends(require_admin)):
    """存量导入①：IDC 游离账号（未在映射表中）+ 归属建议。管理员点按钮触发。"""
    from app.resolver import ImportService

    # 归属建议只在还没有账号的飞书用户里找（每人一个账号）
    return ImportService().list_unlinked(
        known_users=[u for u in _known_feishu_users() if not u.get("has_account")])


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
    from app.resolver import AccountAlreadyLinked, Resolver
    try:
        m = Resolver().manual_link(
            kiro_user_id=body.kiro_user_id, feishu_open_id=body.feishu_open_id,
            feishu_name=body.feishu_name, kiro_username=body.kiro_username,
            kiro_email=body.kiro_email,
        )
    except AccountAlreadyLinked as exc:
        raise HTTPException(400, str(exc))
    return m.__dict__


@router.delete("/mappings/{kiro_user_id}")
def unlink(kiro_user_id: str, _: CurrentUser = Depends(require_admin)):
    MappingStore().delete(kiro_user_id)
    return {"deleted": kiro_user_id}