# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kiro 积分池（credit pool）领域层：池上下文、成员、席位、用量上限、余额总览。

接口口径（2026-11 在真实后付账号实测）：
- 成员：CreateAssignment / DeleteAssignment，subscriptionType=CREDIT_POOL，principalType 为 USER 或 GROUP，
  移出即时生效。ListAssignments 只列直接加入的用户和组（组不展开成员）。
- 席位：ListSeatAssignments 列出全部池用户（含经组加入的），status ACTIVE=已占座、INACTIVE=等待席位
  （控制台显示 Awaiting seat）。后付池加入即自动占座；预付池要调 AssignSeats，没有空席位时返回
  CAPACITY_EXHAUSTED。预付池在控制台首次买席位之前，加人会被拒绝。
- 上限：SetUsageLimit（scope 为 USER / GROUP / POOL；不传 usageLimit 即删除），即时生效。
  生效优先级：用户 > 所在组里最高的组上限 > POOL（profile 默认，按人计）> 无上限。
  上限不能超过池里当前的 credits 总量（月度 + 追加）。
- 余额：ListEntitlements → POOL_SUBSCRIPTION（席位数、每席 credits、本月月度 credits 总量 / 已用，首月按天折算）
  + ADDON（追加 credits，12 个月有效；先用月度 credits，再按到期时间先后用追加 credits）。
- 按人用量没有管理员接口，继续用 User Activity Report（见 app.usage）。
- 买席位 / 买 credits 只能在 Kiro 控制台 checkout 完成，本模块不提供。

设计要点：读接口带短 TTL 缓存，失败由调用方降级；写接口失败抛 PoolError（中文提示，可直接展示）
或 KiroApiError；写操作后失效缓存。
"""
from __future__ import annotations

import calendar
import datetime as _dt
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app import kiro_api
from app.aws import get_session
from app.config import settings
from app.kiro_api import KiroApiError

logger = logging.getLogger(__name__)

SUBSCRIPTION_TYPE = "CREDIT_POOL"
SEAT_PRICE_USD = 20.0
CREDITS_PER_SEAT = 500
ADDON_CREDIT_PRICE_USD = 0.04
LIMIT_SCOPES = ("USER", "GROUP", "POOL")
SEATED, AWAITING_SEAT = "SEATED", "AWAITING_SEAT"

SEAT_REASON_TEXT = {
    "CAPACITY_EXHAUSTED": "没有空余席位：已加入积分池，状态为「等待席位」，请在 Kiro 控制台购买席位后再分配",
    "NOT_ELIGIBLE": "暂时不能分配席位（刚加入积分池时常见），请稍后在管理页重试",
    "SEAT_PENDING_CANCELLATION": "该用户的席位正在等待取消，暂时不能分配",
    "INVALID_SEAT_STATE": "席位状态异常，请在 Kiro 控制台检查",
    "NOT_ADDED_TO_POOL": "用户还没有加入积分池",
}

_CTX_TTL = 600
_READ_TTL = 60
_lock = threading.Lock()
_cache: dict = {}  # key -> (ts, value)


class PoolError(RuntimeError):
    """积分池业务错误（消息可直接展示给管理员）。"""


@dataclass(frozen=True)
class PoolContext:
    account_id: str
    region: str
    instance_arn: str
    pool_arn: str
    payment_model: str  # PREPAID / POSTPAID

    @property
    def prepaid(self) -> bool:
        return self.payment_model == "PREPAID"

    @property
    def seat_plan_code(self) -> str:
        return "KIRO_POOLING_SEAT_PREPAID" if self.prepaid else "KIRO_POOLING_SEAT"


def _cached(key: str, ttl: int, loader, force: bool = False):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < ttl:
            return hit[1]
    value = loader()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def invalidate(*keys: str) -> None:
    """写操作后失效读缓存。不传 key = 除池上下文外全部失效。"""
    with _lock:
        for k in (keys or [k for k in list(_cache) if k != "ctx"]):
            _cache.pop(k, None)


# ---------------------------------------------------------------------------
# 池上下文（账号 / IDC 实例 / 池 ARN / 计费方式）
# ---------------------------------------------------------------------------

def _load_context() -> PoolContext:
    session = get_session()
    region = settings.aws_region
    account_id = settings.kiro_account_id or session.client(
        "sts", region_name=region).get_caller_identity()["Account"]
    instance_arn = settings.sso_instance_arn
    if not instance_arn:
        inst = session.client("sso-admin", region_name=region).list_instances().get("Instances", [])
        if not inst:
            raise PoolError("未找到 IAM Identity Center 实例")
        instance_arn = inst[0]["InstanceArn"]
    pool_arn = settings.kiro_pool_arn
    if not pool_arn:
        for prof in kiro_api.cps("ListProfiles", {}).get("profiles") or []:
            pools = prof.get("creditPools") or []
            if pools and pools[0].get("arn"):
                pool_arn = pools[0]["arn"]
                break
    if not pool_arn:
        raise PoolError("当前 Kiro profile 还没有开启积分池（控制台 Credit sharing）")
    model = kiro_api.cps("GetPaymentModelEligibility", {}).get("paymentModel")
    if model not in ("PREPAID", "POSTPAID"):
        raise PoolError(f"无法确定积分池计费方式（返回 {model!r}）")
    return PoolContext(account_id, region, instance_arn, pool_arn, model)


def get_context(force: bool = False) -> PoolContext:
    return _cached("ctx", _CTX_TTL, _load_context, force)


# ---------------------------------------------------------------------------
# 成员
# ---------------------------------------------------------------------------

def list_direct_members(force: bool = False) -> dict:
    """直接加入池的用户和组：{"users": set, "groups": set}。"""
    def load():
        items = kiro_api.paginate(lambda p: kiro_api.cps("ListAssignments", p),
                                  {"assignmentCategory": SUBSCRIPTION_TYPE}, "assignments")
        return {
            "users": {a["principalId"] for a in items if a.get("principalType") == "USER"},
            "groups": {a["principalId"] for a in items if a.get("principalType") == "GROUP"},
        }
    return _cached("members", _READ_TTL, load, force)


def add_member(principal_id: str, principal_type: str = "USER") -> bool:
    """加入积分池。返回 True = 新加入，False = 已是成员（幂等）。"""
    payload = {"principalId": principal_id, "principalType": principal_type,
               "subscriptionType": SUBSCRIPTION_TYPE}
    try:
        kiro_api.cps("CreateAssignment", payload)
        return True
    except KiroApiError as exc:
        msg = exc.message.lower()
        if "user_onboarding_cap_exceeded" in msg:
            raise PoolError("已达到 profile 可接入用户数上限（Service Quotas：Maximum allowed "
                            "users to onboard per Kiro profile），请提额后重试") from exc
        if exc.kind != "conflict":
            raise
        if "seat purchase" in msg or "not yet ready" in msg:
            raise PoolError("预付积分池还没有购买席位：请先在 Kiro 控制台 Credit sharing 页面"
                            "购买席位，再重试") from exc
        key = "users" if principal_type == "USER" else "groups"
        if principal_id in list_direct_members(force=True)[key]:
            return False
        raise PoolError(f"加入积分池被拒绝：{exc.message}") from exc
    finally:
        invalidate()


def remove_member(principal_id: str, principal_type: str = "USER") -> None:
    """移出积分池（即时生效）。已不在池中视为成功（幂等）。"""
    try:
        kiro_api.cps("DeleteAssignment", {
            "principalId": principal_id, "principalType": principal_type,
            "subscriptionType": SUBSCRIPTION_TYPE, "deletionMode": "IMMEDIATE"})
    except KiroApiError as exc:
        if exc.kind != "not_found":
            raise
    finally:
        invalidate()


# ---------------------------------------------------------------------------
# 席位
# ---------------------------------------------------------------------------

def seat_status(force: bool = False) -> dict:
    """席位实况：每个池用户的占座状态 + 汇总计数（后付池没有 total / available）。"""
    ctx = get_context()

    def load():
        items, summary, token = [], {}, None
        while True:
            p = {"accountId": ctx.account_id, "serviceCode": "Kiro", "planCode": ctx.seat_plan_code,
                 "subscriptionRegion": ctx.region, "instanceArn": ctx.instance_arn,
                 "applicationArn": ctx.pool_arn, "statusFilter": "ALL", "maxResults": 100}
            if token:
                p["nextToken"] = token
            resp = kiro_api.zorn("ListSeatAssignments", p)
            items.extend(resp.get("items") or [])
            if not summary:
                summary = resp
            token = resp.get("nextToken")
            if not token:
                break
        users = {
            i["userId"]: {"status": SEATED if i.get("status") == "ACTIVE" else AWAITING_SEAT,
                          "activated_at": i.get("activatedAt")}
            for i in items if i.get("userId")
        }
        return {
            "users": users,
            "seated": summary.get("activeSeats"),
            "awaiting": summary.get("inactiveUsers"),
            "total": summary.get("totalSeats"),
            "available": summary.get("availableSeats"),
            "pending_removal": summary.get("pendingSeatCount"),
            "pending_removal_effective": summary.get("pendingSeatCountEffectiveDate"),
        }
    return _cached("seats", _READ_TTL, load, force)


def assign_seat(user_id: str, retries: int = 2) -> tuple[bool, str]:
    """给用户分配席位。返回 (是否已占座, 失败原因代码)。后付池自动占座，直接返回 (True, "AUTO")。"""
    ctx = get_context()
    if not ctx.prepaid:
        return True, "AUTO"
    try:
        for attempt in range(retries + 1):
            resp = kiro_api.cps("AssignSeats", {"userIds": [user_id]})
            if any(a.get("userId") == user_id for a in resp.get("successfulAssignments") or []):
                return True, ""
            reason = next((f.get("reason") or "UNKNOWN"
                           for f in resp.get("failedAssignments") or []
                           if f.get("userId") == user_id), "UNKNOWN")
            if reason == "NOT_ELIGIBLE" and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return False, reason
        return False, "UNKNOWN"
    finally:
        invalidate("seats")


def member_status_map(force: bool = False) -> dict:
    """池内用户实况 {user_id: {"status", "direct", "activated_at"}}；不在 dict 中 = 不在池里。"""
    seats = seat_status(force)
    direct = list_direct_members(force)["users"]
    out = {uid: {"status": s["status"], "direct": uid in direct, "activated_at": s.get("activated_at")}
           for uid, s in seats["users"].items()}
    for uid in direct:
        out.setdefault(uid, {"status": AWAITING_SEAT, "direct": True, "activated_at": None})
    return out


# ---------------------------------------------------------------------------
# 用量上限
# ---------------------------------------------------------------------------

def list_limits(force: bool = False) -> list[dict]:
    """全部已设置的上限：[{scope, scope_id, limit, effective_from}]。"""
    def load():
        items = kiro_api.paginate(lambda p: kiro_api.cps("ListUsageLimits", p), {}, "usageLimits")
        return [{"scope": i.get("scope"), "scope_id": i.get("scopeId"),
                 "limit": i.get("usageLimit"), "effective_from": i.get("effectiveFrom")}
                for i in items]
    return _cached("limits", _READ_TTL, load, force)


def profile_limit(force: bool = False) -> float | None:
    """profile 默认上限（每人每账期），未设置返回 None。"""
    return next((x["limit"] for x in list_limits(force) if x["scope"] == "POOL"), None)


def set_limit(scope: str, scope_id: str, limit: float | None) -> dict:
    """设置 / 删除上限（limit=None 即删除，0 = 禁止使用）。即时生效。"""
    scope = (scope or "").upper()
    if scope not in LIMIT_SCOPES:
        raise PoolError(f"无效的上限范围：{scope}（可选 USER / GROUP / POOL）")
    ctx = get_context()
    if scope == "POOL":
        scope_id = ctx.account_id
    if not scope_id:
        raise PoolError("缺少上限对象（用户 / 组 ID）")
    if limit is not None:
        if limit < 0:
            raise PoolError("上限不能为负数")
        total = None
        try:
            total = overview().get("total_quantity")
        except Exception:  # noqa: BLE001 - 余额拿不到时不做前置校验，交给服务端
            logger.warning("set_limit: 余额不可用，跳过总量校验")
        if total is not None and limit > total:
            raise PoolError(f"上限 {limit:g} 超过了积分池当前的 credits 总量（{total:g}），"
                            f"请先在 Kiro 控制台增加席位或购买 credits")
    payload = {"scope": scope, "scopeId": scope_id, "resourceType": "CREDIT",
               "clientToken": str(uuid.uuid4())}
    if limit is not None:
        payload["usageLimit"] = float(limit)
    try:
        return kiro_api.cps("SetUsageLimit", payload)
    finally:
        invalidate("limits", "effective")


def effective_limit(user_id: str) -> dict | None:
    """某用户的生效上限 {limit, scope, scope_id}；None = 无上限。"""
    ctx = get_context()
    resp = kiro_api.zorn("GetEffectiveUsageLimit", {
        "appCode": "Kiro", "accountId": ctx.account_id, "instanceArn": ctx.instance_arn,
        "userId": user_id, "resourceType": "CREDIT", "subscriptionRegion": ctx.region})
    if resp.get("effectiveLimit") is None:
        return None
    src = resp.get("source") or {}
    return {"limit": resp["effectiveLimit"], "scope": src.get("scope"), "scope_id": src.get("scopeId")}


def effective_limits(user_ids: list[str], force: bool = False) -> dict:
    """批量生效上限 {user_id: {...} 或 None}。查询失败的用户不出现在结果里（调用方显示 —）。"""
    ids = sorted({u for u in user_ids if u})
    if not ids:
        return {}
    with _lock:
        hit = _cache.get("effective")
    if hit and not force and time.time() - hit[0] < _READ_TTL and all(u in hit[1] for u in ids):
        return {u: hit[1][u] for u in ids}
    get_context()  # 先在主线程解析上下文，避免线程池里并发加载

    def one(uid: str):
        try:
            return uid, effective_limit(uid), True
        except Exception:  # noqa: BLE001
            logger.warning("effective_limit 查询失败: %s", uid)
            return uid, None, False

    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as ex:
        rows = list(ex.map(one, ids))
    data = {uid: val for uid, val, ok in rows if ok}
    with _lock:
        _cache["effective"] = (time.time(), data)
    return data


# ---------------------------------------------------------------------------
# 余额总览
# ---------------------------------------------------------------------------

def _cycle_bounds(now: _dt.datetime) -> tuple[_dt.datetime, _dt.datetime]:
    """账期 = UTC 自然月。"""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start + _dt.timedelta(days=calendar.monthrange(now.year, now.month)[1])
    return start, end


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc).isoformat()


def overview(force: bool = False, now: _dt.datetime | None = None) -> dict:
    """池余额 + 本账期消耗速度 + 预计耗尽时间。"""
    ctx = get_context()

    def load():
        resp = kiro_api.zorn("ListEntitlements", {
            "principal": {"pool": ctx.account_id}, "ownerAccountId": ctx.account_id,
            "appCode": "Kiro", "subscriptionRegion": ctx.region, "include": ["USAGE"]})
        return resp.get("items") or []

    items = _cached("entitlements", _READ_TTL, load, force)
    return summarize(items, ctx, now or _dt.datetime.now(_dt.timezone.utc))


def summarize(items: list[dict], ctx: PoolContext, now: _dt.datetime) -> dict:
    """把 ListEntitlements 条目汇总成管理页用的口径（纯函数，便于测试）。"""
    m_q = m_used = 0.0
    seat_count, per_seat, m_start = 0, CREDITS_PER_SEAT, None
    addons = []
    for it in items:
        det = it.get("details") or {}
        if it.get("entitlementType") == "POOL_SUBSCRIPTION":
            ps = det.get("poolSubscription") or {}
            u = ps.get("usage") or {}
            m_q += float(u.get("quantity") or 0)
            m_used += float(u.get("currentUsage") or 0)
            seat_count += int(ps.get("seatCount") or 0)
            per_seat = ps.get("quotaPerSeat") or per_seat
            eff = it.get("effectiveAt")
            if eff is not None:
                m_start = min(float(eff), m_start) if m_start is not None else float(eff)
        elif it.get("entitlementType") == "ADDON":
            ad = det.get("addon") or {}
            u = ad.get("usage") or {}
            q, used = float(u.get("quantity") or 0), float(u.get("currentUsage") or 0)
            addons.append({"id": it.get("entitlementId"), "quantity": q, "used": used,
                           "left": max(q - used, 0.0), "source": ad.get("source"),
                           "purchased_at": _iso(it.get("effectiveAt")),
                           "expires_at": _iso(it.get("expiresAt")),
                           "_expires_ts": float(it["expiresAt"]) if it.get("expiresAt") else None})
    start, end = _cycle_bounds(now)
    m_left = max(m_q - m_used, 0.0)
    a_q = sum(a["quantity"] for a in addons)
    a_used = sum(a["used"] for a in addons)
    a_left = sum(a["left"] for a in addons)
    expiring = sum(a["left"] for a in addons
                   if a["_expires_ts"] is not None and a["_expires_ts"] < end.timestamp())
    for a in addons:
        a.pop("_expires_ts", None)

    # 消耗速度：本账期月度 credits 已用 ÷ 已过天数（池中途开通时从开通时刻算）。
    # 月度 credits 用完后，追加 credits 的本月消耗拿不到，此时速度只是下限。
    since = max(start.timestamp(), m_start) if m_start else start.timestamp()
    elapsed_days = max((now.timestamp() - since) / 86400, 1 / 24)
    avg_daily = m_used / elapsed_days if m_used > 0 else 0.0
    days_left = max((end - now).total_seconds() / 86400, 0.0)
    monthly_runout = pool_runout = None
    if avg_daily > 0:
        if m_left / avg_daily < days_left:
            monthly_runout = (now + _dt.timedelta(days=m_left / avg_daily)).isoformat()
        if (m_left + a_left) / avg_daily < days_left:
            pool_runout = (now + _dt.timedelta(days=(m_left + a_left) / avg_daily)).isoformat()
    return {
        "payment_model": ctx.payment_model,
        "prepaid": ctx.prepaid,
        "cycle_start": start.isoformat(),
        "cycle_end": end.isoformat(),
        "days_left": round(days_left, 1),
        "monthly": {"quantity": m_q, "used": m_used, "left": m_left,
                    "seat_count": seat_count, "credits_per_seat": per_seat},
        "addon": {"quantity": a_q, "used": a_used, "left": a_left,
                  "expiring_this_cycle": expiring, "items": addons},
        "total_quantity": m_q + a_q,
        "total_left": m_left + a_left,
        "avg_daily_used": round(avg_daily, 2),
        "rate_is_lower_bound": m_q > 0 and m_left == 0,
        "monthly_runout_at": monthly_runout,
        "pool_runout_at": pool_runout,
        "seat_price_usd": SEAT_PRICE_USD,
        "seat_fee_full_month_usd": seat_count * SEAT_PRICE_USD,
        "addon_credit_price_usd": ADDON_CREDIT_PRICE_USD,
    }


def list_credit_purchases(force: bool = False) -> list[dict]:
    """追加 credits 购买记录（控制台 checkout 买的）。"""
    def load():
        items = kiro_api.paginate(lambda p: kiro_api.cps("ListCreditPurchases", p), {},
                                  "creditPurchases")
        return [{"id": i.get("entitlementId"), "quantity": i.get("quantity"),
                 "status": i.get("status"), "source": i.get("source"),
                 "created_at": _iso(i.get("createdAt")), "expires_at": _iso(i.get("expiresAt"))}
                for i in items]
    return _cached("purchases", _READ_TTL, load, force)
