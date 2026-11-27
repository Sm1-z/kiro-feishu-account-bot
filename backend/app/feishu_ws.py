# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""飞书 WebSocket 长连接：接收卡片审批回调，免公网域名。

设计 03/A3：lark-oapi WS 长连接接收 card.action.trigger。
关键：卡片回调有 ~3s 超时，handle_card_action 只做本地校验并立刻回 toast，
不碰任何 AWS 调用；读申请 / 防重抢占 / 开通 / 更新卡片全部在后台线程完成
（_process_card_action）。AWS 偶尔变慢或凭证刷新（本地用 credential_process 时每次 ~3s）也不会超时。
"""
from __future__ import annotations

import logging
import threading

from app import cards
from app.approval import ApprovalService
from app.config import settings
from app.request_store import APPROVED, EXECUTED, PENDING

logger = logging.getLogger(__name__)


def _run_execution(request_id: str, reviewer_name: str) -> None:
    """后台线程：执行已 approved 的申请 + 通知。"""
    try:
        svc = ApprovalService()
        req = svc.execute_approved(request_id)
        if req:
            svc.notify_after_execution(req, reviewer_name)
    except Exception:
        logger.exception("后台执行审批失败: %s", request_id)


def _update_cards(req, card: dict) -> None:
    from app import feishu
    for mid in req.notify_message_ids:
        try:
            feishu.update_card(mid, card)
        except Exception:
            logger.exception("更新审批卡片失败: %s", mid)


def _process_card_action(action: str, request_id: str, open_id: str) -> None:
    """后台线程：读申请 → 防重抢占 → 执行 → 更新所有审批卡片 / 通知申请人。"""
    try:
        svc = ApprovalService()
        req = svc.requests.get(request_id)
        if not req:
            logger.warning("卡片回调：申请不存在 %s", request_id)
            return
        reviewer_name = open_id  # 简化：用 open_id 作展示名（可后续查通讯录）
        if req.status != PENDING:
            # 已被处理（另一位管理员 / Web 后台）：把卡片刷成无按钮终态，防其它端按钮回退
            _update_cards(req, cards.build_final_card(
                req.type, req.user_name, req.payload, "管理员",
                approved=req.status in (APPROVED, EXECUTED)))
            return
        if action == "approve":
            if not svc.claim_approve(request_id, open_id):
                return
            _update_cards(req, cards.build_processing_card(
                req.type, req.user_name, req.payload, reviewer_name))
            _run_execution(request_id, reviewer_name)
        else:
            if not svc.claim_reject(request_id, open_id):
                return
            req = svc.requests.get(request_id) or req
            svc.notify_after_execution(req, reviewer_name)  # 拒绝：刷终态卡片 + 通知申请人
    except Exception:
        logger.exception("处理卡片回调失败: %s", request_id)


def handle_card_action(data):
    """卡片按钮回调。飞书要求约 3s 内响应：这里只做本地校验并立刻回 toast，其余交给后台线程。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast, P2CardActionTriggerResponse,
    )

    event = data.event
    open_id = event.operator.open_id if event.operator else ""
    value = (event.action.value if event.action else None) or {}

    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    resp.toast = toast

    if open_id not in settings.admin_id_set():
        toast.type = "error"
        toast.content = "你不是管理员，无法审批"
        return resp

    action = value.get("action", "")
    request_id = value.get("request_id")
    if not request_id or action not in ("approve", "reject"):
        toast.type = "info"
        toast.content = "未知操作"
        return resp

    threading.Thread(target=_process_card_action, args=(action, request_id, open_id),
                     daemon=True).start()
    toast.type = "info"
    toast.content = "已受理，正在处理…" if action == "approve" else "已受理，正在处理拒绝…"
    return resp


def start_ws_client() -> None:
    """后台线程启动飞书 WS 长连接。"""

    def _run():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        import lark_oapi as lark
        # Monkey-patch: SDK 默认对 CARD 消息直接 return，需让其走 event_handler
        import base64
        import http
        import time as _t
        from lark_oapi.core.const import UTF_8
        from lark_oapi.core.json import JSON
        from lark_oapi.core.log import logger as lark_logger
        from lark_oapi.ws.client import Client as WsClient, _get_by_key
        from lark_oapi.ws.const import (
            HEADER_BIZ_RT, HEADER_MESSAGE_ID, HEADER_SEQ, HEADER_SUM,
            HEADER_TRACE_ID, HEADER_TYPE,
        )
        from lark_oapi.ws.enum import FrameType, MessageType
        from lark_oapi.ws.model import Response

        async def _patched(self, frame):
            hs = frame.headers
            msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
            trace_id = _get_by_key(hs, HEADER_TRACE_ID)
            sum_ = _get_by_key(hs, HEADER_SUM)
            seq = _get_by_key(hs, HEADER_SEQ)
            type_ = _get_by_key(hs, HEADER_TYPE)
            pl = frame.payload
            if int(sum_) > 1:
                pl = self._combine(msg_id, int(sum_), int(seq), pl)
                if pl is None:
                    return
            mt = MessageType(type_)
            resp = Response(code=http.HTTPStatus.OK)
            try:
                start = int(round(_t.time() * 1000))
                if mt in (MessageType.EVENT, MessageType.CARD):
                    # lark-oapi 1.4.15 该方法名无前导下划线（do_without_validation）
                    result = self._event_handler.do_without_validation(pl)
                else:
                    return
                end = int(round(_t.time() * 1000))
                h = hs.add(); h.key = HEADER_BIZ_RT; h.value = str(end - start)
                if result is not None:
                    resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
            except Exception as e:
                lark_logger.error("handle WS message failed: %s", e)
                resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)
            frame.payload = JSON.marshal(resp).encode(UTF_8)
            frame.method = FrameType.DATA.value
            await self._write_message(frame.SerializeToString())

        WsClient._handle_data_frame = _patched

        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_card_action_trigger(handle_card_action).build())
        cli = lark.ws.Client(app_id=settings.feishu_app_id,
                             app_secret=settings.feishu_app_secret,
                             event_handler=handler, log_level=lark.LogLevel.INFO)
        cli.start()

    threading.Thread(target=_run, daemon=True).start()
    logger.info("飞书 WebSocket 长连接客户端已启动")