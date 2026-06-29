# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kiro 用量分析（对接 Analytics Dashboard）。

数据源与 kiro-user-analytics-dashboard 同一套：Kiro 活动 CSV → Glue → Athena。
本模块按 IDC UserId 聚合用量，供管理页给每个账号映射拼接用量（应用层 JOIN，
关联键 = kiro_user_id = Athena 表的 userid）。

设计要点：
- **只读、降级安全**：Athena 未配置 / 查询失败 → 返回空 dict，管理页用量列显示 —，
  绝不阻断账号列表本身。
- **TTL 缓存**：Athena 查询有秒级延迟且按扫描量计费，不能每次开页面都打。
  默认缓存 5 分钟（用量是天级粒度，无需实时）。
- 凭证走 app.aws 默认链（与全局一致，无 AK/SK）。
"""
from __future__ import annotations

import logging
import threading
import time

from app.aws import get_session
from app.config import settings

logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # 秒
_cache: dict = {"ts": 0.0, "data": {}}
_lock = threading.Lock()


def _enabled() -> bool:
    return bool(settings.athena_database and settings.athena_output_bucket)


def _resolve_table() -> str:
    """表名：优先配置，否则从 Glue 库自动发现首张表。"""
    if settings.glue_table_name:
        return settings.glue_table_name
    glue = get_session().client("glue", region_name=settings.aws_region)
    tables = glue.get_tables(DatabaseName=settings.athena_database, MaxResults=1)
    tl = tables.get("TableList", [])
    if not tl:
        raise RuntimeError(f"Glue 库 {settings.athena_database} 无表，先跑 crawler")
    return tl[0]["Name"]


def _run_query(query: str) -> list[list[str]]:
    """执行 Athena 查询，轮询至完成，返回数据行（不含表头）。"""
    client = get_session().client("athena", region_name=settings.aws_region)
    qid = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": settings.athena_database},
        ResultConfiguration={"OutputLocation": settings.athena_output_bucket},
    )["QueryExecutionId"]
    while True:
        st = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = st["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if state != "SUCCEEDED":
        raise RuntimeError(f"Athena 查询失败: {st.get('StateChangeReason', state)}")
    result = client.get_query_results(QueryExecutionId=qid)
    rows = result["ResultSet"]["Rows"][1:]  # 跳过表头
    return [[c.get("VarCharValue", "") for c in r["Data"]] for r in rows]


def _query_usage_by_user() -> dict:
    """按 userid 聚合用量。返回 {userid: {...}}。"""
    table = _resolve_table()
    # table 名来自配置或 Glue API（非用户输入），无注入风险；
    # Athena 不支持表名参数化，故用字面插值。
    select = (
        "SELECT userid, "
        "SUM(TRY_CAST(total_messages AS INTEGER)) AS messages, "
        "SUM(TRY_CAST(chat_conversations AS INTEGER)) AS conversations, "
        "SUM(TRY_CAST(credits_used AS DOUBLE)) AS credits, "
        "SUM(TRY_CAST(overage_credits_used AS DOUBLE)) AS overage, "
        "MAX(date) AS last_active, "
        "COUNT(DISTINCT date) AS active_days "
    )
    query = select + f"FROM {table} GROUP BY userid"  # nosec B608
    out: dict = {}
    for row in _run_query(query):
        uid = (row[0] or "").strip().strip('"').strip("'")
        if not uid:
            continue
        out[uid] = {
            "messages": _to_int(row[1]),
            "conversations": _to_int(row[2]),
            "credits": _to_float(row[3]),
            "overage": _to_float(row[4]),
            "last_active": row[5] or "",
            "active_days": _to_int(row[6]),
        }
    return out


def get_usage_by_user(force: bool = False) -> dict:
    """用量字典 {userid: {...}}，带 TTL 缓存。失败/未配置返回空 dict（降级）。"""
    if not _enabled():
        return {}
    now = time.time()
    with _lock:
        if not force and (now - _cache["ts"]) < _CACHE_TTL and _cache["data"]:
            return _cache["data"]
    try:
        data = _query_usage_by_user()
        with _lock:
            _cache["ts"], _cache["data"] = time.time(), data
        return data
    except Exception:
        logger.exception("拉取 Kiro 用量失败，降级为空")
        # 失败时返回上次缓存（若有），否则空
        return _cache["data"] or {}


def _to_int(v, default=0):
    try:
        return int(float(v)) if v not in (None, "", "None") else default
    except (ValueError, TypeError):
        return default


def _to_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "None") else default
    except (ValueError, TypeError):
        return default