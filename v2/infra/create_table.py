"""创建 DynamoDB 映射表 kiro-account-mapping。

用法（凭证走 IAM Role / aws sso）：
    python infra/create_table.py

表结构（设计 06-data-layer.md）：
    PK:  kiro_user_id (S)
    GSI: feishu_open_id-index  HASH=feishu_open_id (S)
    计费: PAY_PER_REQUEST（按量，此数据量月费 ≈ $0）
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import boto3  # noqa: E402
from app.config import settings  # noqa: E402


def create_table():
    ddb = boto3.client("dynamodb", region_name=settings.aws_region)
    table = settings.mapping_table_name
    gsi = settings.mapping_gsi_name

    existing = ddb.list_tables().get("TableNames", [])
    if table in existing:
        print(f"表 {table} 已存在，跳过。")
        return

    ddb.create_table(
        TableName=table,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "kiro_user_id", "AttributeType": "S"},
            {"AttributeName": "feishu_open_id", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "kiro_user_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": gsi,
                "KeySchema": [{"AttributeName": "feishu_open_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    print(f"正在创建表 {table}（含 GSI {gsi}）...")
    ddb.get_waiter("table_exists").wait(TableName=table)
    print("完成。")


if __name__ == "__main__":
    create_table()
