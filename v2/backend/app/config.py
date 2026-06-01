"""配置层。

安全基线（v2 设计 04-improvements D1/D2）：
- **不读 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY**。boto3 一律走默认凭证链，
  在 ECS/EKS/EC2 上自动解析为 Task Role / IRSA / Instance Profile。
- 仅暴露资源标识（region / identity_store_id / sso_instance_arn）等非密配置。

凭证从环境变量注入是反模式：见设计文档「安全基线」。本模块刻意不提供任何
读取长期密钥的入口。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- AWS（仅资源标识，无凭证）----
    aws_region: str = Field(default="us-east-1")
    identity_store_id: str = Field(default="")  # 空则运行时用 sso-admin:ListInstances 解析
    sso_instance_arn: str = Field(default="")
    kiro_sign_in_url: str = Field(default="")
    kiro_group_name: str = Field(default="Q")

    # ---- DynamoDB 映射表 ----
    mapping_table_name: str = Field(default="kiro-account-mapping")
    mapping_gsi_name: str = Field(default="feishu_open_id-index")

    # ---- 账号配额 ----
    default_account_quota: int = Field(default=2)

    # ---- 飞书 ----
    feishu_app_id: str = Field(default="")
    feishu_app_secret: str = Field(default="")
    feishu_redirect_uri: str = Field(default="")
    admin_open_ids: str = Field(default="")  # 逗号分隔

    # ---- Web ----
    jwt_secret: str = Field(default="")
    frontend_url: str = Field(default="http://localhost:5173")

    def admin_id_set(self) -> set[str]:
        return {s.strip() for s in self.admin_open_ids.split(",") if s.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
