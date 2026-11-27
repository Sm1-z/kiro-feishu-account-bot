"""kiro_api 测试：错误分类、限流重试、翻页、端点与签名名。"""
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import kiro_api as K  # noqa: E402


def _http_error(code, body):
    e = urllib.error.HTTPError("http://x", code, "err", {}, None)
    e.read = lambda: body.encode()
    return e


def test_classify_json_type_and_plain_text():
    assert K._classify(400, json.dumps({"__type": "com.amazon.x#ConflictException",
                                        "message": "exists"})) == ("conflict", "exists")
    assert K._classify(404, "ResourceNotFoundException: nope")[0] == "not_found"
    assert K._classify(429, "slow down")[0] == "throttled"
    assert K._classify(400, json.dumps({"__type": "ValidationException",
                                        "message": "Improperly formed request."}))[0] == "validation"
    assert K._classify(500, "boom")[0] == "other"


def test_signed_post_raises_kiro_api_error():
    with patch("app.kiro_api.urllib.request.urlopen",
               side_effect=_http_error(400, json.dumps({"__type": "ConflictException",
                                                        "message": "dup"}))):
        with pytest.raises(K.KiroApiError) as ei:
            K.signed_post("https://x/", "Svc.CreateAssignment", "q", {}, credentials=MagicMock(
                access_key="a", secret_key="b", token=None))
    assert ei.value.kind == "conflict" and ei.value.op == "CreateAssignment"


def test_cps_uses_management_endpoint_and_q_signing():
    with patch("app.kiro_api.signed_post", return_value={"ok": 1}) as sp:
        assert K.cps("ListAssignments", {"a": 1}) == {"ok": 1}
    url, target, service, payload = sp.call_args.args
    assert url.startswith("https://management.") and url.endswith(".kiro.dev/")
    assert target == "KiroControlPlaneQDeveloperService.ListAssignments"
    assert service == "q" and payload == {"a": 1}


def test_zorn_uses_user_subscriptions_signing():
    with patch("app.kiro_api.signed_post", return_value={}) as sp:
        K.zorn("ListSeatAssignments", {})
    url, target, service, _ = sp.call_args.args
    assert "service.user-subscriptions." in url
    assert target == "AWSZornControlPlaneService.ListSeatAssignments"
    assert service == "user-subscriptions"


def test_retry_on_throttle_then_success():
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise K.KiroApiError("Op", 429, "throttled", "slow")
        return {"done": True}

    with patch("app.kiro_api.signed_post", side_effect=flaky), patch("app.kiro_api.time.sleep"):
        assert K.cps("Op") == {"done": True}
    assert calls["n"] == 3


def test_no_retry_on_conflict():
    with patch("app.kiro_api.signed_post",
               side_effect=K.KiroApiError("Op", 400, "conflict", "dup")) as sp, \
         patch("app.kiro_api.time.sleep"):
        with pytest.raises(K.KiroApiError):
            K.cps("Op")
    assert sp.call_count == 1


def test_paginate_follows_next_token():
    pages = [{"items": [1, 2], "nextToken": "t1"}, {"items": [3]}]
    seen = []

    def call(p):
        seen.append(p)
        return pages[len(seen) - 1]

    assert K.paginate(call, {"x": 1}, "items") == [1, 2, 3]
    assert seen[0] == {"x": 1, "maxResults": 100}
    assert seen[1]["nextToken"] == "t1"
