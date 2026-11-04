"""usage 按月聚合：SQL 月份过滤、userid 去引号、缓存、降级。"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.usage as U  # noqa: E402

ROWS = [
    ['"44f8b438-6011-706c-84a0-1742588956ff"', "89.45694312232172", "0.0", "73", "1", "2026-11-03"],
    ["'u-2'", "12.5", "3.0", "10", "2", "2026-11-02"],
    ["", "1", "0", "1", "1", "2026-11-01"],  # 空 userid 跳过
]


@pytest.fixture(autouse=True)
def _clear():
    U._month_cache.clear()
    yield
    U._month_cache.clear()


def test_month_key_validation():
    assert U.month_key(2026, 11) == "2026-11"
    assert U.month_key(2026, 1) == "2026-01"
    with pytest.raises(ValueError):
        U.month_key(2026, 13)


def test_month_query_filters_month_and_strips_quotes():
    with patch.object(U, "_enabled", return_value=True), \
         patch.object(U, "_resolve_table", return_value="us_east_1"), \
         patch.object(U, "_run_query", return_value=ROWS) as rq:
        out = U.get_month_usage_by_user(2026, 11)
    sql = rq.call_args.args[0]
    assert "substr(date, 1, 7) = '2026-11'" in sql and "GROUP BY userid" in sql
    assert set(out) == {"44f8b438-6011-706c-84a0-1742588956ff", "u-2"}
    me = out["44f8b438-6011-706c-84a0-1742588956ff"]
    assert me["credits"] == pytest.approx(89.4569, abs=1e-3)
    assert me["overage"] == 0.0 and me["messages"] == 73 and me["active_days"] == 1
    assert me["last_active"] == "2026-11-03"
    assert out["u-2"]["overage"] == 3.0


def test_month_cache_per_month_and_force():
    with patch.object(U, "_enabled", return_value=True), \
         patch.object(U, "_resolve_table", return_value="t"), \
         patch.object(U, "_run_query", return_value=ROWS) as rq:
        U.get_month_usage_by_user(2026, 11)
        U.get_month_usage_by_user(2026, 11)          # 命中缓存
        assert rq.call_count == 1
        U.get_month_usage_by_user(2026, 10)          # 不同月份独立缓存
        assert rq.call_count == 2
        U.get_month_usage_by_user(2026, 11, force=True)
        assert rq.call_count == 3


def test_month_query_degrades_to_empty_or_last_cache():
    with patch.object(U, "_enabled", return_value=True), \
         patch.object(U, "_resolve_table", return_value="t"), \
         patch.object(U, "_run_query", side_effect=RuntimeError("athena down")):
        assert U.get_month_usage_by_user(2026, 11) == {}
    # 有旧缓存时失败返回旧缓存
    U._month_cache["2026-11"] = {"ts": 0.0, "data": {"u": {"credits": 1.0}}}
    with patch.object(U, "_enabled", return_value=True), \
         patch.object(U, "_resolve_table", return_value="t"), \
         patch.object(U, "_run_query", side_effect=RuntimeError("athena down")):
        assert U.get_month_usage_by_user(2026, 11) == {"u": {"credits": 1.0}}


def test_month_query_disabled_returns_empty():
    with patch.object(U, "_enabled", return_value=False), patch.object(U, "_run_query") as rq:
        assert U.get_month_usage_by_user(2026, 11) == {}
        rq.assert_not_called()


def test_current_month_helper():
    y, m = U.current_month()
    assert 2026 <= y <= 2100 and 1 <= m <= 12
    with patch.object(U, "get_month_usage_by_user", return_value={"x": 1}) as g:
        assert U.get_current_month_usage_by_user(force=True) == {"x": 1}
        assert g.call_args.args[:2] == (y, m) and g.call_args.kwargs["force"] is True
