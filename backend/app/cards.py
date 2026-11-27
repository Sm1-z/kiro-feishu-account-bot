# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""飞书交互卡片模板。

- 审批卡片：推给管理员，含「通过 / 拒绝」按钮，action.value 带 request_id
- 处理中/通过/拒绝卡片：审批后替换原卡片，移除按钮
- 结果通知卡片：通知申请人
"""
from __future__ import annotations

TYPE_LABEL = {"apply": "开通账号", "limit_change": "调整用量上限", "quota_increase": "增加配额"}


def _limit_text(v) -> str:
    return f"{v} credits / 月" if v not in (None, "") else "默认（组上限或 profile 默认上限）"


def _fields(req_type: str, applicant: str, payload: dict, pool_hint: str = "") -> list[dict]:
    rows = [f"**申请人**：{applicant}", f"**类型**：{TYPE_LABEL.get(req_type, req_type)}"]
    if req_type == "apply":
        rows += [
            f"**用户名**：{payload.get('username', '')}",
            f"**邮箱**：{payload.get('email', '')}",
            f"**分组**：{payload.get('group', '')}",
            f"**用量上限**：{_limit_text(payload.get('usage_limit'))}",
        ]
    elif req_type == "limit_change":
        rows += [
            f"**用户名**：{payload.get('username', '')}",
            f"**当前上限**：{_limit_text(payload.get('current_limit'))}",
            f"**申请上限**：{_limit_text(payload.get('requested_limit'))}",
        ]
    elif req_type == "quota_increase":
        rows.append(f"**申请配额**：{payload.get('requested_quota', '')}")
    if payload.get("reason"):
        rows.append(f"**理由**：{payload['reason']}")
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rows)}}]
    if pool_hint:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": pool_hint}]})
    return elements


def build_approval_card(request_id: str, req_type: str, applicant: str, payload: dict,
                        pool_hint: str = "") -> dict:
    """待审批卡片（含按钮）。pool_hint：积分池现状一句话（剩余 credits / 席位），帮助审批人判断。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": f"🔔 Kiro 申请待审批 · {TYPE_LABEL.get(req_type, req_type)}"}},
        "elements": _fields(req_type, applicant, payload, pool_hint) + [
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "✅ 通过"},
                 "type": "primary", "value": {"action": "approve", "request_id": request_id}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                 "type": "danger", "value": {"action": "reject", "request_id": request_id}},
            ]},
        ],
    }


def build_processing_card(req_type: str, applicant: str, payload: dict, reviewer: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": f"⏳ 处理中 · {TYPE_LABEL.get(req_type, req_type)}"}},
        "elements": _fields(req_type, applicant, payload) + [
            {"tag": "note", "elements": [{"tag": "plain_text", "content": f"审批人：{reviewer} · 正在执行开通…"}]},
        ],
    }


def build_final_card(req_type: str, applicant: str, payload: dict, reviewer: str,
                     approved: bool, detail: str = "") -> dict:
    template = "green" if approved else "red"
    icon = "✅ 已通过" if approved else "❌ 已拒绝"
    note = f"审批人：{reviewer}"
    if detail:
        note += f" · {detail}"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template,
                   "title": {"tag": "plain_text", "content": f"{icon} · {TYPE_LABEL.get(req_type, req_type)}"}},
        "elements": _fields(req_type, applicant, payload) + [
            {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]},
        ],
    }


def build_user_result_card(req_type: str, payload: dict, success: bool,
                           sign_in_url: str = "", reason: str = "", warning: str = "") -> dict:
    """通知申请人结果。warning：成功但有待办（如预付池暂无空余席位）。"""
    if success:
        rows = [f"你的「{TYPE_LABEL.get(req_type, req_type)}」申请已通过 ✅"]
        if req_type == "apply":
            rows.append(f"**用户名**：{payload.get('username', '')}")
            if sign_in_url:
                rows.append(f"**登录地址**：{sign_in_url}")
            rows.append("请查收邮箱中的密码设置邮件。")
        elif req_type == "limit_change":
            rows.append(f"**新上限**：{_limit_text(payload.get('requested_limit'))}（已即时生效）")
        if warning:
            rows.append(f"⚠️ {warning}")
        template, title = "green", "✅ 申请已通过"
    else:
        rows = [f"你的「{TYPE_LABEL.get(req_type, req_type)}」申请未通过 ❌"]
        if reason:
            rows.append(f"**原因**：{reason}")
        template, title = "red", "❌ 申请未通过"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rows)}}],
    }