"""审批执行引擎。

把「审批通过后做什么」从 WS/HTTP 入口中剥离出来，便于测试与复用。

关键设计（设计 02 安全基线）：
- 防重：先用 RequestStore.transition(pending→approved) 条件更新抢占，失败说明
  已被处理，直接返回，不重复执行。
- 异步：execute_approved() 跑 AWS provision/upgrade（耗时），由调用方放到后台线程，
  使飞书卡片回调能在 ~3s 内先返回。
- 锚点：开通成功后用 provision 返回的 UserId 写 DynamoDB 映射（resolver）。
"""
from __future__ import annotations

import logging

from app import cards, feishu, provisioner
from app.config import settings
from app.mapping_store import MappingStore
from app.request_store import (
    APPLY, APPROVED, EXECUTED, FAILED, PENDING, QUOTA_INCREASE, REJECTED, UPGRADE,
    Request, RequestStore,
)
from app.resolver import Resolver

logger = logging.getLogger(__name__)


class ApprovalService:
    def __init__(self, requests: RequestStore | None = None,
                 mapping: MappingStore | None = None):
        self.requests = requests or RequestStore()
        self.mapping = mapping or MappingStore()
        self.resolver = Resolver(self.mapping)

    # ---- 审批入口（防重抢占）----

    def claim_approve(self, request_id: str, reviewer_open_id: str) -> bool:
        """抢占审批权：pending→approved。返回 True=本次抢到（应继续执行）。"""
        import time
        return self.requests.transition(
            request_id, PENDING, APPROVED,
            reviewer_open_id=reviewer_open_id, reviewed_at=int(time.time()),
        )

    def claim_reject(self, request_id: str, reviewer_open_id: str, comment: str = "") -> bool:
        import time
        return self.requests.transition(
            request_id, PENDING, REJECTED,
            reviewer_open_id=reviewer_open_id, reviewed_at=int(time.time()),
            review_comment=comment or "",
        )

    # ---- 异步执行（已 approved 后调用，跑 AWS）----

    def execute_approved(self, request_id: str) -> Request | None:
        """执行已通过的申请。耗时，调用方应放后台线程。"""
        req = self.requests.get(request_id)
        if not req or req.status != APPROVED:
            logger.warning("execute_approved: 申请 %s 不存在或非 approved", request_id)
            return req

        if req.type == APPLY:
            self._exec_apply(req)
        elif req.type == UPGRADE:
            self._exec_upgrade(req)
        elif req.type == QUOTA_INCREASE:
            self._exec_quota(req)

        return self.requests.get(request_id)

    def _exec_apply(self, req: Request) -> None:
        p = req.payload
        result = provisioner.provision(
            username=p["username"], email=p["email"],
            given_name=p.get("given_name", ""), family_name=p.get("family_name", ""),
            tier=p["tier"], group_name=p.get("group", settings.kiro_group_name),
        )
        if result.success:
            # 写映射：UserId 锚点 + 主/副自动判定
            self.resolver.record_new_account(
                kiro_user_id=result.user_id,
                feishu_open_id=req.user_open_id,
                feishu_name=req.user_name,
                kiro_username=p["username"],
                kiro_email=p["email"],
                tier=p["tier"],
                team=p.get("group", ""),
                approved_by=req.reviewer_open_id,
            )
            self.requests.update_fields(
                req.request_id, status=EXECUTED,
                result={"user_id": result.user_id, "steps": result.steps_succeeded},
            )
        else:
            self.requests.update_fields(
                req.request_id, status=FAILED,
                result={"error": result.error, "error_step": result.error_step},
            )

    def _exec_upgrade(self, req: Request) -> None:
        p = req.payload
        result = provisioner.upgrade(username=p["username"], tier=p["target_tier"])
        if result.success:
            if req.kiro_user_id:
                self.mapping.update_fields(req.kiro_user_id, tier=p["target_tier"])
            self.requests.update_fields(
                req.request_id, status=EXECUTED, result={"new_tier": p["target_tier"]})
        else:
            self.requests.update_fields(
                req.request_id, status=FAILED, result={"error": result.error})

    def _exec_quota(self, req: Request) -> None:
        # 配额变更不涉及 AWS，直接记录（实际配额存于 user 维度，见路由层）
        self.requests.update_fields(
            req.request_id, status=EXECUTED,
            result={"new_quota": req.payload.get("requested_quota")})

    # ---- 通知（卡片更新 + 通知申请人）----

    def notify_after_execution(self, req: Request, reviewer_name: str) -> None:
        """执行后更新审批卡片 + 通知申请人。失败不影响主流程。"""
        ok = req.status == EXECUTED
        detail = "" if ok else (req.result or {}).get("error", "执行失败")
        try:
            final = cards.build_final_card(req.type, req.user_name, req.payload,
                                           reviewer_name, approved=ok, detail=detail)
            for mid in req.notify_message_ids:
                feishu.update_card(mid, final)
        except Exception:
            logger.exception("更新审批卡片失败: %s", req.request_id)
        try:
            user_card = cards.build_user_result_card(
                req.type, req.payload, success=ok,
                sign_in_url=settings.kiro_sign_in_url, reason=detail)
            feishu.send_card(req.user_open_id, user_card)
        except Exception:
            logger.exception("通知申请人失败: %s", req.request_id)
