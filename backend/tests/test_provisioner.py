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
    # TIER_MAP 现用 KIRO_ENTERPRISE_*（Claim 模型 type.amazonQ）
    assert P.TIER_MAP["pro"] == "KIRO_ENTERPRISE_PRO"
    assert P.TIER_MAP["pro max"] == "KIRO_ENTERPRISE_PRO_MAX"
    assert P.TIER_MAP["power"] == "KIRO_ENTERPRISE_POWER"
    # 反向映射覆盖 enterprise（新）与 standalone（历史）两套 plan code
    assert P.TIER_REVERSE["KIRO_ENTERPRISE_PRO_PLUS"] == "pro+"
    assert P.TIER_REVERSE["KIRO_ENTERPRISE_PRO_MAX"] == "pro max"
    assert P.TIER_REVERSE["Q_DEVELOPER_STANDALONE_PRO_MAX"] == "pro max"
    assert P.TIER_DISPLAY["pro max"] == "Kiro Pro Max"


def test_tier_of_subscription_both_shapes():
    # 订阅读回为 KIRO_ENTERPRISE_*（Claim 模型 / ListUserSubscriptions）；
    # 旧的 Q_DEVELOPER_STANDALONE_* 反查仍保留以兼容历史数据。
    assert P._tier_of_subscription({"type": {"amazonQ": "KIRO_ENTERPRISE_PRO"}}) == "pro"
    assert P._tier_of_subscription({"type": {"amazonQ": "KIRO_ENTERPRISE_POWER"}}) == "power"
    assert P._tier_of_subscription({"type": {"amazonQ": "KIRO_ENTERPRISE_PRO_MAX"}}) == "pro max"
    assert P._tier_of_subscription({"activatedType": {"amazonQ": "Q_DEVELOPER_STANDALONE_PRO"}}) == "pro"
    assert P._tier_of_subscription({}) == ""


# CreateClaim 内部会查 application ARN / instance ARN，单测里统一 mock 掉。
def _patch_claim_helpers():
    return (
        patch.object(P, "_get_kiro_application_arn", return_value="arn:aws:sso::1:application/i/apl-x"),
        patch.object(P, "_resolve_instance_arn", return_value="arn:aws:sso:::instance/i"),
        patch("app.provisioner.get_session", return_value=MagicMock()),
    )


def test_create_assignment_idempotent_conflict():
    """已订阅 → ConflictException → 返回 False，不报错。"""
    p1, p2, p3 = _patch_claim_helpers()
    with p1, p2, p3, patch.object(P, "_signed_post",
                                  side_effect=_http_error(409, "ConflictException: exists")):
        assert P._create_assignment("u1", "KIRO_ENTERPRISE_PRO", MagicMock(), "us-east-1") is False


def test_create_assignment_retries_on_429():
    """429 退避重试，最终成功。"""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429, "TooManyRequests")
        return b"{}"

    p1, p2, p3 = _patch_claim_helpers()
    with p1, p2, p3, patch.object(P, "_signed_post", side_effect=flaky), \
         patch("app.provisioner.time.sleep"):  # 跳过真实 sleep
        assert P._create_assignment("u1", "KIRO_ENTERPRISE_PRO", MagicMock(), "us-east-1", max_retries=3) is True
    assert calls["n"] == 3


def test_create_assignment_gives_up_after_max_retries():
    p1, p2, p3 = _patch_claim_helpers()
    with p1, p2, p3, patch.object(P, "_signed_post", side_effect=_http_error(429, "TooManyRequests")), \
         patch("app.provisioner.time.sleep"):
        with pytest.raises(RuntimeError):
            P._create_assignment("u1", "KIRO_ENTERPRISE_PRO", MagicMock(), "us-east-1", max_retries=2)


def test_create_assignment_sends_claim_payload():
    """CreateClaim 应携带 Claim 模型的关键字段（principal.user / type.amazonQ / applicationArn）。"""
    captured = {}

    def cap(url, target, svc, region, payload, creds):
        captured.update(target=target, payload=payload)
        return b"{}"

    p1, p2, p3 = _patch_claim_helpers()
    with p1, p2, p3, patch.object(P, "_signed_post", side_effect=cap):
        assert P._create_assignment("u-abc", "KIRO_ENTERPRISE_PRO_MAX", MagicMock(), "us-east-1") is True
    assert captured["target"].endswith("CreateClaim")
    pl = captured["payload"]
    assert pl["principal"] == {"user": "u-abc"}
    assert pl["type"] == {"amazonQ": "KIRO_ENTERPRISE_PRO_MAX"}
    assert pl["identityProvider"] == "IDENTITY_STORE"
    assert "applicationArn" in pl


def test_delete_assignment_idempotent_no_claim():
    """找不到 claim（已无订阅）→ 跳过 DeleteClaim，仅幂等解绑 application，不抛。"""
    sso = MagicMock()
    session = MagicMock()
    session.client.return_value = sso
    with patch.object(P, "_find_claim", return_value=None), \
         patch.object(P, "_get_kiro_application_arn", return_value="arn:app"), \
         patch("app.provisioner.get_session", return_value=session):
        P._delete_assignment("u1", MagicMock(), "us-east-1")  # 不应抛
    sso.delete_application_assignment.assert_called_once()


def test_delete_assignment_deletes_claim_when_present():
    """存在 claim → 调 DeleteClaim（带 subscription ARN）+ 解绑 application。"""
    claim = {"identifier": "arn:aws:user-subscriptions::1:subscription/abc"}
    captured = {}

    def cap(url, target, svc, region, payload, creds):
        captured.setdefault("targets", []).append(target)
        captured["last_payload"] = payload
        return b"{}"

    sso = MagicMock()
    session = MagicMock()
    session.client.return_value = sso
    with patch.object(P, "_find_claim", return_value=claim), \
         patch.object(P, "_get_kiro_application_arn", return_value="arn:app"), \
         patch("app.provisioner.get_session", return_value=session), \
         patch.object(P, "_signed_post", side_effect=cap):
        P._delete_assignment("u1", MagicMock(), "us-east-1")
    assert any(t.endswith("DeleteClaim") for t in captured["targets"])
    assert captured["last_payload"]["identifier"] == claim["identifier"]
    sso.delete_application_assignment.assert_called_once()


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


def test_bulk_cancel_also_deletes_mapping():
    """回收账号时：退订 + 删 IDC 用户 + 删映射三者同步，避免孤儿映射。"""
    idc = MagicMock()
    session = MagicMock()
    session.client.return_value = idc
    mapping = MagicMock()

    with patch("app.provisioner.get_session", return_value=session), \
         patch("app.provisioner.get_identity_store_id", return_value="d-1"), \
         patch("app.provisioner.get_frozen_credentials", return_value=MagicMock()), \
         patch("app.mapping_store.MappingStore", return_value=mapping), \
         patch.object(P, "_delete_assignment"):
        res = P.bulk_cancel(["uid-1", "uid-2"])

    assert all(r["success"] for r in res)
    idc.delete_user.assert_any_call(IdentityStoreId="d-1", UserId="uid-1")
    # 关键：映射也被删（修复孤儿记录 bug）
    mapping.delete.assert_any_call("uid-1")
    mapping.delete.assert_any_call("uid-2")
    assert mapping.delete.call_count == 2
