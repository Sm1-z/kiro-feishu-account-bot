"""Kiro 账号开通/变更/取消/查询 核心工作流。

设计要点（安全基线）：
- 凭证全部走 app.aws（IAM Role），无任何 AK/SK
- 开通成功返回 UserId（稳定锚点），供上层写入 DynamoDB 映射
- 分步错误定位 + 批量并发；全步骤幂等；订阅 429 指数退避重试

涉及两个**非公开内部 API**（手动 SigV4 直连，AWS 变更可能失效，见设计 04 D3）：
- SWBUPService.UpdatePassword       发送密码设置邮件
- AmazonQDeveloperService.*Assignment  订阅管理
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import botocore.auth
import botocore.awsrequest
from botocore.exceptions import ClientError

from app.aws import get_frozen_credentials, get_identity_store_id, get_session
from app.config import settings

logger = logging.getLogger(__name__)

# 订阅走 user-subscriptions 的 Claim 模型（CreateClaim/UpdateClaim/DeleteClaim），
# type.amazonQ 用 KIRO_ENTERPRISE_* 命名（与 ListUserSubscriptions/Console 一致）。
# 旧的 q:CreateAssignment（Q_DEVELOPER_STANDALONE_*）已被 Kiro 控制面弃用，不再使用。
TIER_MAP = {
    "pro": "KIRO_ENTERPRISE_PRO",
    "pro+": "KIRO_ENTERPRISE_PRO_PLUS",
    "pro max": "KIRO_ENTERPRISE_PRO_MAX",  # Kiro Pro Max ($100)
    "power": "KIRO_ENTERPRISE_POWER",
}
TIER_DISPLAY = {
    "pro": "Kiro Pro",
    "pro+": "Kiro Pro+",
    "pro max": "Kiro Pro Max",
    "power": "Kiro Power",
}
TIER_REVERSE = {
    "Q_DEVELOPER_STANDALONE_PRO": "pro",
    "Q_DEVELOPER_STANDALONE_PRO_PLUS": "pro+",
    "Q_DEVELOPER_STANDALONE_PRO_MAX": "pro max",
    "Q_DEVELOPER_STANDALONE_POWER": "power",
    "KIRO_ENTERPRISE_PRO": "pro",
    "KIRO_ENTERPRISE_PRO_PLUS": "pro+",
    "KIRO_ENTERPRISE_PRO_MAX": "pro max",
    "KIRO_ENTERPRISE_POWER": "power",
}


@dataclass
class ProvisionResult:
    success: bool
    user_id: str = ""
    error: str = ""
    error_step: str = ""
    steps_succeeded: list = field(default_factory=list)


@dataclass
class SimpleResult:
    success: bool
    error: str = ""


# ---------------------------------------------------------------------------
# 内部签名 API helper（SigV4 手签直连）
# ---------------------------------------------------------------------------

def _signed_post(url: str, target: str, service: str, region: str,
                 payload: dict, credentials) -> bytes:
    """对内部 JSON-1.0 API 做 SigV4 签名并 POST，返回响应体。"""
    body = json.dumps(payload)
    headers = {
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": target,
    }
    req = botocore.awsrequest.AWSRequest(method="POST", url=url, data=body, headers=headers)
    botocore.auth.SigV4Auth(credentials, service, region).add_auth(req)
    http_req = urllib.request.Request(
        url, data=body.encode(), headers=dict(req.headers), method="POST"
    )
    return urllib.request.urlopen(http_req, timeout=30).read()


def _send_password_reset(user_id: str, credentials, region: str) -> None:
    try:
        _signed_post(
            f"https://identitystore.{region}.amazonaws.com/",
            "SWBUPService.UpdatePassword", "userpool", region,
            {"UserId": user_id, "PasswordMode": "EMAIL"}, credentials,
        )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"UpdatePassword failed: HTTP {exc.code}: {exc.read().decode()}") from exc


_US_SVC = "user-subscriptions"


def _us_url(region: str) -> str:
    return f"https://service.user-subscriptions.{region}.amazonaws.com/"


def _us_target(op: str) -> str:
    return f"AWSZornControlPlaneService.{op}"


_app_arn_cache: dict = {}


def _get_kiro_application_arn(credentials, region: str) -> str:
    """获取 Kiro 订阅用的 application ARN（QDevProfile-*）。带进程内缓存。

    订阅的 Claim 模型挂在 IdC application 上；该 ARN 通过 sso-admin:ListApplications
    查 Name 以 'QDevProfile' 开头的那个（即 Q Developer / Kiro 订阅 profile）。
    """
    if region in _app_arn_cache:
        return _app_arn_cache[region]
    session = get_session()
    sso = session.client("sso-admin", region_name=region)
    instance_arn = settings.sso_instance_arn or _resolve_instance_arn(sso)
    paginator = sso.get_paginator("list_applications") if sso.can_paginate("list_applications") else None
    apps = []
    if paginator:
        for page in paginator.paginate(InstanceArn=instance_arn):
            apps.extend(page.get("Applications", []))
    else:
        apps = sso.list_applications(InstanceArn=instance_arn).get("Applications", [])
    for a in apps:
        if (a.get("Name") or "").startswith("QDevProfile"):
            _app_arn_cache[region] = a["ApplicationArn"]
            return a["ApplicationArn"]
    raise RuntimeError("未找到 Kiro 订阅 application（QDevProfile-*）；请确认 IdC 已启用 Kiro")


def _resolve_instance_arn(sso) -> str:
    insts = sso.list_instances().get("Instances", [])
    if not insts:
        raise RuntimeError("无 IAM Identity Center 实例")
    return insts[0]["InstanceArn"]


def _find_claim(user_id: str, credentials, region: str) -> dict | None:
    """在 Kiro application 的 claims 里按 principal.user 找到该用户的订阅 claim。

    返回含 identifier(subscription ARN) 与 type 的 claim dict；无则 None。
    """
    app_arn = _get_kiro_application_arn(credentials, region)
    next_token = None
    while True:
        payload = {"applicationArn": app_arn, "maxResults": 100, "subscriptionRegion": region}
        if next_token:
            payload["nextToken"] = next_token
        raw = _signed_post(_us_url(region), _us_target("ListApplicationClaims"), _US_SVC,
                           region, payload, credentials)
        data = json.loads(raw.decode())
        for c in data.get("claims", []):
            if c.get("principal", {}).get("user") == user_id:
                return c
        next_token = data.get("nextToken")
        if not next_token:
            return None


def _create_assignment(user_id: str, sub_type: str, credentials, region: str,
                       max_retries: int = 3) -> bool:
    """订阅（Claim 模型 CreateClaim）。已订阅(ConflictException)视为幂等成功；
    429 指数退避。返回 True=新订阅、False=已存在。

    sub_type 为 KIRO_ENTERPRISE_* 取值（见 TIER_MAP）。
    """
    app_arn = _get_kiro_application_arn(credentials, region)
    instance_arn = settings.sso_instance_arn or _resolve_instance_arn(
        get_session().client("sso-admin", region_name=region))
    payload = {
        "instanceArn": instance_arn,
        "principal": {"user": user_id},
        "applicationArn": app_arn,
        "transaction": True,
        "type": {"amazonQ": sub_type},
        "identityProvider": "IDENTITY_STORE",
        "createForSelf": False,
        "subscriptionRegion": region,
    }
    for attempt in range(max_retries + 1):
        try:
            _signed_post(_us_url(region), _us_target("CreateClaim"), _US_SVC,
                         region, payload, credentials)
            return True
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            if "ConflictException" in body:
                return False
            if exc.code == 429 and attempt < max_retries:
                time.sleep(min(5 * 2 ** attempt, 60))
                continue
            raise RuntimeError(f"CreateClaim failed: HTTP {exc.code}: {body}") from exc
    raise RuntimeError("CreateClaim: max retries exceeded")


def _update_assignment(user_id: str, sub_type: str, credentials, region: str) -> None:
    """升级/变更套餐（Claim 模型 UpdateClaim）。需先按 user 找到 subscription ARN。"""
    claim = _find_claim(user_id, credentials, region)
    if not claim:
        raise RuntimeError("升级失败：未找到该账号的 Kiro 订阅（可能尚未开通）。")
    payload = {
        "identifier": claim["identifier"],
        "transaction": False,
        "type": {"amazonQ": sub_type},
        "subscriptionRegion": region,
    }
    try:
        _signed_post(_us_url(region), _us_target("UpdateClaim"), _US_SVC,
                     region, payload, credentials)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"UpdateClaim failed: HTTP {exc.code}: {body}") from exc


def _delete_assignment(user_id: str, credentials, region: str) -> None:
    """取消订阅（Claim 模型）。先 DeleteClaim 退订，再 DeleteApplicationAssignment
    解除 application 绑定。找不到 claim 视为已无订阅(幂等)。"""
    claim = _find_claim(user_id, credentials, region)
    if claim:
        try:
            _signed_post(_us_url(region), _us_target("DeleteClaim"), _US_SVC, region,
                         {"identifier": claim["identifier"], "transaction": True,
                          "subscriptionRegion": region}, credentials)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            if "ResourceNotFoundException" not in body:
                raise RuntimeError(f"DeleteClaim failed: HTTP {exc.code}: {body}") from exc
    # 解除 application 分配（sso 服务）。已解除/不存在视为幂等。
    app_arn = _get_kiro_application_arn(credentials, region)
    try:
        session = get_session()
        sso = session.client("sso-admin", region_name=region)
        sso.delete_application_assignment(
            ApplicationArn=app_arn, PrincipalId=user_id, PrincipalType="USER")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("ResourceNotFoundException",):
            raise


def _list_subscriptions(credentials, region: str) -> list[dict]:
    instance_arn = settings.sso_instance_arn or _resolve_instance_arn(
        get_session().client("sso-admin", region_name=region))
    try:
        raw = _signed_post(
            _us_url(region),
            _us_target("ListUserSubscriptions"), _US_SVC,
            region,
            {"instanceArn": instance_arn, "maxResults": 1000,
             "subscriptionRegion": region},
            credentials,
        )
        return json.loads(raw.decode()).get("subscriptions", [])
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ListUserSubscriptions failed: HTTP {exc.code}: {exc.read().decode()}") from exc


def _tier_of_subscription(sub: dict) -> str:
    plan = sub.get("activatedType", {}).get("amazonQ", "") or sub.get("type", {}).get("amazonQ", "")
    return TIER_REVERSE.get(plan, "")


# ---------------------------------------------------------------------------
# IDC helper
# ---------------------------------------------------------------------------

def _get_user_id_by_name(idc, id_store: str, username: str) -> str:
    resp = idc.get_user_id(
        IdentityStoreId=id_store,
        AlternateIdentifier={
            "UniqueAttribute": {"AttributePath": "userName", "AttributeValue": username}
        },
    )
    return resp["UserId"]


def _create_user(idc, id_store, username, given_name, family_name, email) -> tuple[str, bool]:
    """创建 IDC 用户。已存在(同名)抛错防认领他人账号。返回 (user_id, created)。"""
    try:
        resp = idc.create_user(
            IdentityStoreId=id_store,
            UserName=username,
            Name={"GivenName": given_name or username, "FamilyName": family_name or username},
            DisplayName=f"{given_name} {family_name}".strip() or username,
            Emails=[{"Value": email, "Type": "Work", "Primary": True}],
        )
        return resp["UserId"], True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConflictException":
            raise RuntimeError(f"用户名 '{username}' 在 IDC 中已存在，请更换用户名") from exc
        raise


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def provision(username: str, email: str, given_name: str, family_name: str,
              tier: str, group_name: str) -> ProvisionResult:
    """开通 Kiro 账号（4 步幂等）。成功后 result.user_id 即稳定锚点。"""
    tier_key = tier.strip().lower()
    sub_type = TIER_MAP.get(tier_key)
    if not sub_type:
        return ProvisionResult(success=False, error=f"无效套餐: {tier}", error_step="validate")

    region = settings.aws_region
    result = ProvisionResult(success=False)
    try:
        session = get_session()
        id_store = get_identity_store_id(session)
        idc = session.client("identitystore", region_name=region)
        creds = get_frozen_credentials()

        # 1) 创建用户
        try:
            user_id, created = _create_user(idc, id_store, username, given_name, family_name, email)
            result.user_id = user_id
            result.steps_succeeded.append("user_created" if created else "user_exists")
        except Exception as exc:
            result.error, result.error_step = str(exc), "create_user"
            return result

        # 2) 加组
        try:
            gid = idc.get_group_id(
                IdentityStoreId=id_store,
                AlternateIdentifier={"UniqueAttribute": {
                    "AttributePath": "displayName", "AttributeValue": group_name}},
            )["GroupId"]
            try:
                idc.create_group_membership(
                    IdentityStoreId=id_store, GroupId=gid, MemberId={"UserId": user_id})
                result.steps_succeeded.append("group_added")
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConflictException":
                    result.steps_succeeded.append("group_already_member")
                else:
                    raise
        except Exception as exc:
            result.error, result.error_step = f"加入组 '{group_name}' 失败: {exc}", "add_to_group"
            return result

        # 3) 密码邮件
        try:
            _send_password_reset(user_id, creds, region)
            result.steps_succeeded.append("password_email_sent")
        except Exception as exc:
            result.error, result.error_step = f"发送密码邮件失败: {exc}", "send_password"
            return result

        # 4) 订阅
        try:
            new_sub = _create_assignment(user_id, sub_type, creds, region)
            result.steps_succeeded.append("subscription_created" if new_sub else "already_subscribed")
        except Exception as exc:
            result.error, result.error_step = f"订阅 Kiro 失败: {exc}", "kiro_subscribe"
            return result

        result.success = True
        return result
    except Exception as exc:
        result.error, result.error_step = f"未预期错误: {exc}", "unknown"
        logger.exception("provision failed")
        return result


def upgrade(username: str, tier: str) -> SimpleResult:
    tier_key = tier.strip().lower()
    sub_type = TIER_MAP.get(tier_key)
    if not sub_type:
        return SimpleResult(success=False, error=f"无效套餐: {tier}")
    region = settings.aws_region
    try:
        session = get_session()
        id_store = get_identity_store_id(session)
        idc = session.client("identitystore", region_name=region)
        user_id = _get_user_id_by_name(idc, id_store, username)
        _update_assignment(user_id, sub_type, get_frozen_credentials(), region)
        return SimpleResult(success=True)
    except Exception as exc:
        return SimpleResult(success=False, error=str(exc))


def cancel(username: str) -> SimpleResult:
    region = settings.aws_region
    try:
        session = get_session()
        id_store = get_identity_store_id(session)
        idc = session.client("identitystore", region_name=region)
        user_id = _get_user_id_by_name(idc, id_store, username)
        _delete_assignment(user_id, get_frozen_credentials(), region)
        return SimpleResult(success=True)
    except Exception as exc:
        return SimpleResult(success=False, error=str(exc))


def query_tier(username: str) -> str | None:
    """查询用户当前 tier（pro/pro+/power）或 None。"""
    region = settings.aws_region
    try:
        session = get_session()
        id_store = get_identity_store_id(session)
        idc = session.client("identitystore", region_name=region)
        creds = get_frozen_credentials()
        user_id = _get_user_id_by_name(idc, id_store, username)
        for sub in _list_subscriptions(creds, region):
            if sub.get("principal", {}).get("user") == user_id and \
               sub.get("status") in ("ACTIVE", "PENDING"):
                return _tier_of_subscription(sub)
        return None
    except Exception:
        return None


# ---- 批量（管理员运维 / 副账号回收）----

def bulk_update_tier(user_ids: list[str], tier: str) -> list[dict]:
    sub_type = TIER_MAP.get(tier.strip().lower())
    if not sub_type:
        return [{"user_id": u, "success": False, "error": f"无效套餐: {tier}"} for u in user_ids]
    region = settings.aws_region
    creds = get_frozen_credentials()

    def _do(uid):
        try:
            _update_assignment(uid, sub_type, creds, region)
            return {"user_id": uid, "success": True, "error": ""}
        except Exception as exc:
            return {"user_id": uid, "success": False, "error": str(exc)}

    if not user_ids:
        return []
    with ThreadPoolExecutor(max_workers=min(10, len(user_ids))) as ex:
        return list(ex.map(_do, user_ids))


def bulk_cancel(user_ids: list[str]) -> list[dict]:
    """批量取消订阅 + 删 IDC 用户 + 删 DynamoDB 映射（副账号回收）。

    三件事必须同步，否则映射表会残留孤儿记录（IDC 已删但映射还在），
    导致 dashboard 显示幽灵账号、对其升级/操作报 USER not found。
    取消订阅时 ResourceNotFound 视为成功（幂等）。
    """
    from app.mapping_store import MappingStore

    region = settings.aws_region
    session = get_session()
    id_store = get_identity_store_id(session)
    idc = session.client("identitystore", region_name=region)
    creds = get_frozen_credentials()
    mapping = MappingStore()
    results = []
    for uid in user_ids:
        try:
            _delete_assignment(uid, creds, region)
            idc.delete_user(IdentityStoreId=id_store, UserId=uid)
            mapping.delete(uid)  # 同步清映射，避免孤儿记录
            results.append({"user_id": uid, "success": True, "error": ""})
        except Exception as exc:
            results.append({"user_id": uid, "success": False, "error": str(exc)})
    return results
