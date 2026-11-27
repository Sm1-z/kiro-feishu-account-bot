# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kiro quota 只读查询测试：正常查询、回退默认值、单项降级、TTL 缓存。"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.quotas as Q  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    Q._cache["ts"], Q._cache["data"] = 0.0, None
    yield
    Q._cache["ts"], Q._cache["data"] = 0.0, None


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetServiceQuota")


def _session(client):
    s = MagicMock()
    s.client.return_value = client
    return s


def _quota(code, value):
    return {"Quota": {"QuotaCode": code, "QuotaName": f"name-{code}", "Value": value,
                      "Adjustable": True}}


def test_get_kiro_quotas_ok():
    client = MagicMock()
    client.get_service_quota.side_effect = lambda ServiceCode, QuotaCode: _quota(
        QuotaCode, {"L-295A3FFB": 1000.0, "L-05AF9163": 2500000.0}[QuotaCode])
    with patch("app.quotas.get_session", return_value=_session(client)):
        q = Q.get_kiro_quotas()
    assert q["onboarding_cap"]["value"] == 1000.0
    assert q["monthly_purchase_cap"]["value"] == 2500000.0
    assert q["onboarding_cap"]["console_url"].endswith("/kiro/quotas/L-295A3FFB")


def test_fallback_to_default_and_per_item_degrade():
    client = MagicMock()

    def get(ServiceCode, QuotaCode):
        if QuotaCode == "L-295A3FFB":
            raise _client_error("NoSuchResourceException")
        raise _client_error("AccessDeniedException")

    client.get_service_quota.side_effect = get
    client.get_aws_default_service_quota.return_value = _quota("L-295A3FFB", 0.0)
    with patch("app.quotas.get_session", return_value=_session(client)):
        q = Q.get_kiro_quotas()
    assert q["onboarding_cap"]["value"] == 0.0
    assert q["monthly_purchase_cap"] is None  # 单项失败不影响其它项


def test_cache_hit_skips_api_and_force_refreshes():
    client = MagicMock()
    client.get_service_quota.side_effect = lambda ServiceCode, QuotaCode: _quota(QuotaCode, 1.0)
    with patch("app.quotas.get_session", return_value=_session(client)):
        Q.get_kiro_quotas()
        Q.get_kiro_quotas()
        assert client.get_service_quota.call_count == 2  # 两项各一次，第二次走缓存
        Q.get_kiro_quotas(force=True)
        assert client.get_service_quota.call_count == 4
