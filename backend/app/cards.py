# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""飞书交互卡片模板。

- 审批卡片：推给管理员，含「通过 / 拒绝」按钮，action.value 带 request_id
- 处理中/通过/拒绝卡片：审批后替换原卡片，移除按钮
- 结果通知卡片：通知申请人
"""
from __future__ import annotations

TYPE_LABEL = {"apply": "开通账号", "upgrade": "升级套餐", "quota_increase": "增加配额"}
TIER_LABEL = {"pro": "Kiro Pro", "pro+": "Kiro Pro+", "pro max": "Kiro Pro Max", "power": "Kiro Power"}


def _fields(req_type: str, applicant: str, payload: dict) -> list[dict]:
    rows = [f"**申请人**：{applicant}", f"**类型**：{TYPE_LABEL.get(req_type, req_type)}"]
    if req_type == "apply":
        rows += [
            f"**用户名**：{payload.get('username', '')}",
            f"**邮箱**：{payload.get('email', '')}",
            f"**分组**：{payload.get('group', '')}",
            f"**套餐**：{TIER_LABEL.get(payload.get('tier', ''), payload.get('tier', ''))}",
        ]
    elif req_type == "upgrade":
        rows += [
            f"**用户名**：{payload.get('username', '')}",
            f"**目标套餐**：{TIER_LABEL.get(payload.get('target_tier', ''), payload.get('target_tier', ''))}",
        ]
    elif req_type == "quota_increase":
        rows.append(f"**申请配额**：{payload.get('requested_quota', '')}")
    return [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rows)}}]


def build_approval_card(request_id: str, req_type: str, applicant: str, payload: dict) -> dict:
    """待审批卡片（含按钮）。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": f"🔔 Kiro 申请待审批 · {TYPE_LABEL.get(req_type, req_type)}"}},
        "elements": _fields(req_type, applicant, payload) + [
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
                           sign_in_url: str = "", reason: str = "") -> dict:
    """通知申请人结果。"""
    if success:
        rows = [f"你的「{TYPE_LABEL.get(req_type, req_type)}」申请已通过 ✅"]
        if req_type == "apply":
            rows.append(f"**用户名**：{payload.get('username', '')}")
            if sign_in_url:
                rows.append(f"**登录地址**：{sign_in_url}")
            rows.append("请查收邮箱中的密码设置邮件。")
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