"""Provisioner 逻辑测试。

mock 掉内部签名 API 与 IDC client，验证：tier 映射、订阅 tier 解析、
幂等（ConflictException→False）、429 退避重试、开通分步错误定位。
"""
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.provisioner as P  # noqa: E402


def _http_error(code, body):
    e = urllib.error.HTTPError("http://x", code, "err", {}, None)
    e.read = lambda: body.encode()
    return e


def test_tier_maps_complete():
    assert set(P.TIER_MAP) == {"pro", "pro+", "pro max", "power"}
    # 反向映射覆盖 standalone 与 enterprise 两套 plan code
    assert P.TIER_REVERSE["Q_DEVELOPER_STANDALONE_POWER"] == "power"
    assert P.TIER_REVERSE["KIRO_ENTERPRISE_PRO_PLUS"] == "pro+"
    # 新增 Kiro Pro Max ($100)：两套 plan code 均能反查
    assert P.TIER_REVERSE["Q_DEVELOPER_STANDALONE_PRO_MAX"] == "pro max"
    assert P.TIER_REVERSE["KIRO_ENTERPRISE_PRO_MAX"] == "pro max"
    assert P.TIER_DISPLAY["pro max"] == "Kiro Pro Max"


def test_tier_of_subscription_both_shapes():
    assert P._tier_of_subscription({"activatedType": {"amazonQ": "Q_DEVELOPER_STANDALONE_PRO"}}) == "pro"
    assert P._tier_of_subscription({"type": {"amazonQ": "Q_DEVELOPER_STANDALONE_POWER"}}) == "power"
    assert P._tier_of_subscription({}) == ""


def test_create_assignment_idempotent_conflict():
    """已订阅 → ConflictException → 返回 False，不报错。"""
    with patch.object(P, "_signed_post", side_effect=_http_error(409, "ConflictException: exists")):
        assert P._create_assignment("u1", "Q_DEVELOPER_STANDALONE_PRO", MagicMock(), "us-east-1") is False


def test_create_assignment_retries_on_429():
    """429 退避重试，最终成功。"""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429, "TooManyRequests")
        return b"{}"

    with patch.object(P, "_signed_post", side_effect=flaky), \
         patch("app.provisioner.time.sleep"):  # 跳过真实 sleep
        assert P._create_assignment("u1", "Q_DEVELOPER_STANDALONE_PRO", MagicMock(), "us-east-1", max_retries=3) is True
    assert calls["n"] == 3


def test_create_assignment_gives_up_after_max_retries():
    with patch.object(P, "_signed_post", side_effect=_http_error(429, "TooManyRequests")), \
         patch("app.provisioner.time.sleep"):
        with pytest.raises(RuntimeError):
            P._create_assignment("u1", "Q_DEVELOPER_STANDALONE_PRO", MagicMock(), "us-east-1", max_retries=2)


def test_delete_assignment_idempotent_not_found():
    """已无订阅 → ResourceNotFound → 视为成功，不抛。"""
    with patch.object(P, "_signed_post", side_effect=_http_error(404, "ResourceNotFoundException")):
        P._delete_assignment("u1", MagicMock(), "us-east-1")  # 不应抛


def test_provision_invalid_tier():
    r = P.provision("u", "e@x.com", "G", "F", "deluxe", "Q")
    assert r.success is False
    assert r.error_step == "validate"


def test_provision_happy_path_returns_userid():
    """开通成功返回 UserId（稳定锚点），步骤齐全。"""
    idc = MagicMock()
    idc.create_user.return_value = {"UserId": "test-uid-aaaa"}
    idc.get_group_id.return_value = {"GroupId": "g-1"}
    idc.create_group_membership.return_value = {}
    session = MagicMock()
    session.client.return_value = idc

    with patch("app.provisioner.get_session", return_value=session), \
         patch("app.provisioner.get_identity_store_id", return_value="d-1"), \
         patch("app.provisioner.get_frozen_credentials", return_value=MagicMock()), \
         patch.object(P, "_send_password_reset"), \
         patch.object(P, "_create_assignment", return_value=True):
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "pro+", "Q")

    assert r.success is True
    assert r.user_id == "test-uid-aaaa"
    assert "user_created" in r.steps_succeeded
    assert "subscription_created" in r.steps_succeeded


def test_provision_pro_max_uses_correct_subscription_type():
    """开通 Kiro Pro Max 时，传给 CreateAssignment 的 subscriptionType 必须是 Pro Max 的映射值。"""
    idc = MagicMock()
    idc.create_user.return_value = {"UserId": "test-uid-bbbb"}
    idc.get_group_id.return_value = {"GroupId": "g-1"}
    idc.create_group_membership.return_value = {}
    session = MagicMock()
    session.client.return_value = idc

    with patch("app.provisioner.get_session", return_value=session), \
         patch("app.provisioner.get_identity_store_id", return_value="d-1"), \
         patch("app.provisioner.get_frozen_credentials", return_value=MagicMock()), \
         patch.object(P, "_send_password_reset"), \
         patch.object(P, "_create_assignment", return_value=True) as mock_assign:
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "pro max", "Q")

    assert r.success is True
    # _create_assignment(user_id, sub_type, credentials, region) —— sub_type 为第 2 个位置参数
    assert mock_assign.call_args.args[1] == P.TIER_MAP["pro max"]


def test_provision_duplicate_username_blocks():
    """同名用户名 → create_user 抛 Conflict → error_step=create_user。"""
    from botocore.exceptions import ClientError
    idc = MagicMock()
    idc.create_user.side_effect = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "exists"}}, "CreateUser")
    session = MagicMock()
    session.client.return_value = idc

    with patch("app.provisioner.get_session", return_value=session), \
         patch("app.provisioner.get_identity_store_id", return_value="d-1"), \
         patch("app.provisioner.get_frozen_credentials", return_value=MagicMock()):
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "pro", "Q")

    assert r.success is False
    assert r.error_step == "create_user"
