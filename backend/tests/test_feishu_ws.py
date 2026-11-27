"""飞书卡片回调测试：回调路径不碰 AWS、立刻返回；后台线程负责抢占 / 执行 / 刷卡片。"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import feishu_ws as W  # noqa: E402
from app.config import settings  # noqa: E402

ADMIN = "ou_admin"


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_open_ids", ADMIN)


def _event(open_id, action="approve", request_id="r1"):
    return SimpleNamespace(event=SimpleNamespace(
        operator=SimpleNamespace(open_id=open_id),
        action=SimpleNamespace(value={"action": action, "request_id": request_id})))


def test_callback_returns_immediately_without_aws():
    started = []
    fake_thread = lambda target, args, daemon: SimpleNamespace(start=lambda: started.append((target, args)))
    with patch("app.feishu_ws.ApprovalService", side_effect=AssertionError("回调路径不应访问 AWS")), \
         patch("app.feishu_ws.threading.Thread", side_effect=fake_thread):
        resp = W.handle_card_action(_event(ADMIN))
    assert resp.toast.type == "info" and "已受理" in resp.toast.content
    assert started == [(W._process_card_action, ("approve", "r1", ADMIN))]


def test_callback_rejects_non_admin_and_unknown_action():
    with patch("app.feishu_ws.threading.Thread") as th:
        assert W.handle_card_action(_event("ou_x")).toast.type == "error"
        assert W.handle_card_action(_event(ADMIN, action="drop")).toast.content == "未知操作"
    th.assert_not_called()


def _req(status="pending"):
    return SimpleNamespace(type="apply", user_name="张三", payload={"username": "zs"},
                           status=status, notify_message_ids=["m1", "m2"], user_open_id="ou_u")


def test_process_approve_claims_marks_processing_and_executes():
    svc = MagicMock()
    svc.requests.get.return_value = _req()
    svc.claim_approve.return_value = True
    with patch("app.feishu_ws.ApprovalService", return_value=svc), \
         patch("app.feishu.update_card") as upd, \
         patch("app.feishu_ws._run_execution") as run:
        W._process_card_action("approve", "r1", ADMIN)
    svc.claim_approve.assert_called_once_with("r1", ADMIN)
    assert upd.call_count == 2  # 两张审批卡片都刷成「处理中」
    run.assert_called_once_with("r1", ADMIN)


def test_process_already_handled_refreshes_cards_only():
    svc = MagicMock()
    svc.requests.get.return_value = _req(status="executed")
    with patch("app.feishu_ws.ApprovalService", return_value=svc), \
         patch("app.feishu.update_card") as upd, \
         patch("app.feishu_ws._run_execution") as run:
        W._process_card_action("approve", "r1", ADMIN)
    svc.claim_approve.assert_not_called()
    run.assert_not_called()
    assert upd.call_count == 2


def test_process_reject_claims_and_notifies():
    svc = MagicMock()
    svc.requests.get.return_value = _req()
    svc.claim_reject.return_value = True
    with patch("app.feishu_ws.ApprovalService", return_value=svc):
        W._process_card_action("reject", "r1", ADMIN)
    svc.claim_reject.assert_called_once_with("r1", ADMIN)
    svc.notify_after_execution.assert_called_once()


def test_process_lost_race_does_nothing():
    svc = MagicMock()
    svc.requests.get.return_value = _req()
    svc.claim_approve.return_value = False
    with patch("app.feishu_ws.ApprovalService", return_value=svc), \
         patch("app.feishu_ws._run_execution") as run:
        W._process_card_action("approve", "r1", ADMIN)
    run.assert_not_called()
