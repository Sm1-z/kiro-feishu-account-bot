"""飞书 WebSocket 长连接：接收卡片审批回调，免公网域名。

设计 03/A3：lark-oapi WS 长连接接收 card.action.trigger。
关键：卡片回调有 ~3s 超时，故 _handle_card_action 立即返回「处理中」卡片，
真正的 AWS 开通在后台线程跑（threading.Thread daemon）。
"""
from __future__ import annotations

import logging
import threading

from app import cards
from app.approval import ApprovalService
from app.config import settings
from app.request_store import APPROVED, EXECUTED, PENDING, REJECTED

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


def handle_card_action(data):
    """卡片按钮回调处理。必须快速返回（~3s 超时）。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard, CallBackToast, P2CardActionTriggerResponse,
    )

    event = data.event
    open_id = event.operator.open_id if event.operator else ""
    value = event.action.value if event.action else {}

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

    svc = ApprovalService()
    req = svc.requests.get(request_id)
    if not req:
        toast.type = "error"
        toast.content = "申请不存在"
        return resp
    if req.status != PENDING:
        toast.type = "info"
        toast.content = f"该申请已处理（{req.status}）"
        # 返回无按钮终态卡片，防其它端按钮回退
        final = cards.build_final_card(
            req.type, req.user_name, req.payload, "管理员",
            approved=req.status in (APPROVED, EXECUTED))
        card = CallBackCard(); card.type = "raw"; card.data = final
        resp.card = card
        return resp

    reviewer_name = open_id  # 简化：用 open_id 作展示名（可后续查通讯录）

    if action == "approve":
        if not svc.claim_approve(request_id, open_id):
            toast.type = "info"
            toast.content = "该申请已被处理"
            return resp
        # 后台执行 AWS 开通
        threading.Thread(target=_run_execution, args=(request_id, reviewer_name),
                         daemon=True).start()
        toast.type = "info"
        toast.content = "已受理，正在执行…"
        processing = cards.build_processing_card(req.type, req.user_name, req.payload, reviewer_name)
        card = CallBackCard(); card.type = "raw"; card.data = processing
        resp.card = card
    else:  # reject
        if not svc.claim_reject(request_id, open_id):
            toast.type = "info"
            toast.content = "该申请已被处理"
            return resp
        final = cards.build_final_card(req.type, req.user_name, req.payload,
                                       reviewer_name, approved=False)
        for mid in req.notify_message_ids:
            try:
                from app import feishu
                feishu.update_card(mid, final)
            except Exception:
                pass
        # 通知申请人
        try:
            from app import feishu
            feishu.send_card(req.user_open_id,
                             cards.build_user_result_card(req.type, req.payload, success=False))
        except Exception:
            pass
        toast.type = "success"
        toast.content = "已拒绝"
        card = CallBackCard(); card.type = "raw"; card.data = final
        resp.card = card

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
                    result = self._event_handler._do_without_validation(pl)
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
