# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Overage quota 查询测试。

mock 掉 service-quotas client，验证：正常查询、NoSuchResource 回退默认值、
异常降级返回 None、TTL 缓存命中。
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.quotas as Q  # noqa: E402


def _reset_cache():
    Q._cache["ts"], Q._cache["data"] = 0.0, None


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetServiceQuota")


def _mock_session(client):
    session = MagicMock()
    session.client.return_value = client
    return session


def test_get_overage_cap_ok():
    _reset_cache()
    client = MagicMock()
    client.get_service_quota.return_value = {
        "Quota": {"Value": 400.0, "QuotaName": "Maximum allowed overage per Kiro profile",
                  "Adjustable": True}
    }
    with patch.object(Q, "get_session", return_value=_mock_session(client)):
        cap = Q.get_overage_cap(force=True)
    assert cap["value"] == 400.0
    assert cap["adjustable"] is True
    assert Q.OVERAGE_QUOTA_CODE in cap["console_url"]


def test_fallback_to_default_quota():
    """账号无 applied 值（NoSuchResource）→ 回退查服务默认值。"""
    _reset_cache()
    client = MagicMock()
    client.get_service_quota.side_effect = _client_error("NoSuchResourceException")
    client.get_aws_default_service_quota.return_value = {
        "Quota": {"Value": 0.0, "QuotaName": "x", "Adjustable": True}
    }
    with patch.object(Q, "get_session", return_value=_mock_session(client)):
        cap = Q.get_overage_cap(force=True)
    assert cap["value"] == 0.0
    client.get_aws_default_service_quota.assert_called_once()


def test_degrades_to_none_on_error():
    """无权限等异常 → None，不抛错（管理页降级显示 —）。"""
    _reset_cache()
    client = MagicMock()
    client.get_service_quota.side_effect = _client_error("AccessDeniedException")
    with patch.object(Q, "get_session", return_value=_mock_session(client)):
        assert Q.get_overage_cap(force=True) is None


def test_cache_hit_skips_api():
    _reset_cache()
    client = MagicMock()
    client.get_service_quota.return_value = {
        "Quota": {"Value": 400.0, "QuotaName": "x", "Adjustable": True}
    }
    with patch.object(Q, "get_session", return_value=_mock_session(client)):
        Q.get_overage_cap(force=True)
        Q.get_overage_cap()  # 第二次走缓存
    assert client.get_service_quota.call_count == 1


# ---- 调高（increase-only）----

def _client_with_cap(value=400.0):
    client = MagicMock()
    client.get_service_quota.return_value = {
        "Quota": {"Value": value, "QuotaName": "x", "Adjustable": True}
    }
    client.list_requested_service_quota_change_history_by_quota.return_value = {
        "RequestedQuotas": []
    }
    return client


def test_raise_rejects_not_greater():
    """新值 ≤ 当前值 → ValueError（提前拦截 API 的 IllegalArgumentException）。"""
    _reset_cache()
    client = _client_with_cap(400.0)
    with patch.object(Q, "get_session", return_value=_mock_session(client)):
        with pytest.raises(ValueError, match="必须大于当前值"):
            Q.request_cap_increase(300)
    client.request_service_quota_increase.assert_not_called()


def test_raise_rejects_over_factor():
    """超过当前值 2 倍 → ValueError（防误触拉爆敞口）。"""
    _reset_cache()
    client = _client_with_cap(400.0)
    with patch.object(Q, "get_session", return_value=_mock_session(client)):
        with pytest.raises(ValueError, match="2 倍"):
            Q.request_cap_increase(900)
    client.request_service_quota_increase.assert_not_called()


def test_raise_rejects_when_pending():
    _reset_cache()
    client = _client_with_cap(400.0)
    client.list_requested_service_quota_change_history_by_quota.return_value = {
        "RequestedQuotas": [{"Status": "PENDING", "DesiredValue": 500.0, "Created": None}]
    }
    with patch.object(Q, "get_session", return_value=_mock_session(client)):
        with pytest.raises(ValueError, match="审批中"):
            Q.request_cap_increase(600)
    client.request_service_quota_increase.assert_not_called()


def test_raise_ok_and_invalidates_cache():
    _reset_cache()
    client = _client_with_cap(400.0)
    client.request_service_quota_increase.return_value = {
        "RequestedQuota": {"DesiredValue": 500.0, "Status": "APPROVED", "Id": "rq-1"}
    }
    with patch.object(Q, "get_session", return_value=_mock_session(client)):
        result = Q.request_cap_increase(500)
    assert result == {"desired_value": 500.0, "status": "APPROVED", "request_id": "rq-1"}
    assert Q._cache["ts"] == 0.0  # 缓存失效，下次读取拿新值
