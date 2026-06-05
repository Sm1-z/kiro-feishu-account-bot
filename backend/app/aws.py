"""AWS 会话与凭证。

唯一的 boto3 Session 出口。所有 AWS 调用都从这里取 session / client / 凭证，
保证：
- 凭证一律走 boto3 默认链（Role），**无任何长期 AK/SK 读取路径**
- Identity Store ID 解析逻辑集中（优先配置，否则 sso-admin:ListInstances）
- 内部签名 API（SWBUP / Amazon Q）所需的 frozen credentials 统一获取
"""
from __future__ import annotations

import boto3

from app.config import settings


def get_session() -> boto3.Session:
    """默认凭证链 Session。在 ECS/EKS/EC2 自动解析为 Task Role / IRSA / Instance Profile。"""
    return boto3.Session(region_name=settings.aws_region)


def get_frozen_credentials():
    """内部签名 API（SigV4 手签）需要的冻结凭证。同样来自 Role。"""
    creds = get_session().get_credentials()
    if creds is None:
        raise RuntimeError(
            "无法解析 AWS 凭证。请确认运行环境已绑定 IAM Role "
            "(ECS Task Role / EKS IRSA / EC2 Instance Profile)。"
        )
    return creds.get_frozen_credentials()


def get_identity_store_id(session: boto3.Session | None = None) -> str:
    """获取 Identity Store ID：优先配置，否则用 sso-admin:ListInstances 解析。"""
    if settings.identity_store_id:
        return settings.identity_store_id
    session = session or get_session()
    resp = session.client("sso-admin", region_name=settings.aws_region).list_instances()
    instances = resp.get("Instances", [])
    if not instances:
        raise RuntimeError("未找到 AWS Identity Center 实例")
    return instances[0]["IdentityStoreId"]
