# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""创建 DynamoDB 表：映射表 + 申请/审批表。

用法（凭证走 IAM Role / aws sso / 本地 default profile）：
    python infra/create_table.py

表结构（设计 06-data-layer.md）：
  kiro-account-mapping
    PK:  kiro_user_id (S)
    GSI: feishu_open_id-index  HASH=feishu_open_id (S)
  kiro-account-mapping-requests（申请/审批 + 审计日志，设计 04 A4）
    PK:  request_id (S)
    GSI: user_open_id-index  HASH=user_open_id (S)  —— 免 list_by_user 全表 scan
  计费: PAY_PER_REQUEST（按量，此数据量月费 ≈ $0）
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import boto3  # noqa: E402
from app.config import settings  # noqa: E402


def _create(ddb, name: str, attrs: list, key_schema: list, gsis: list):
    existing = ddb.list_tables().get("TableNames", [])
    if name in existing:
        print(f"表 {name} 已存在，跳过。")
        return
    kwargs = dict(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=attrs,
        KeySchema=key_schema,
    )
    if gsis:
        kwargs["GlobalSecondaryIndexes"] = gsis
    ddb.create_table(**kwargs)
    print(f"正在创建表 {name}...")
    ddb.get_waiter("table_exists").wait(TableName=name)
    print(f"  {name} 完成。")


def create_tables():
    ddb = boto3.client("dynamodb", region_name=settings.aws_region)

    # 1) 映射表
    _create(
        ddb,
        settings.mapping_table_name,
        attrs=[
            {"AttributeName": "kiro_user_id", "AttributeType": "S"},
            {"AttributeName": "feishu_open_id", "AttributeType": "S"},
        ],
        key_schema=[{"AttributeName": "kiro_user_id", "KeyType": "HASH"}],
        gsis=[
            {
                "IndexName": settings.mapping_gsi_name,
                "KeySchema": [{"AttributeName": "feishu_open_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )

    # 2) 申请/审批表（表名与 RequestStore 默认一致：<mapping>-requests）
    _create(
        ddb,
        f"{settings.mapping_table_name}-requests",
        attrs=[
            {"AttributeName": "request_id", "AttributeType": "S"},
            {"AttributeName": "user_open_id", "AttributeType": "S"},
        ],
        key_schema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
        gsis=[
            {
                "IndexName": "user_open_id-index",
                "KeySchema": [{"AttributeName": "user_open_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


if __name__ == "__main__":
    create_tables()