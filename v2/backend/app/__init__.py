"""Kiro 账号管理平台 v2 — 后端核心。

设计文档见 ../../docs/design/。核心范式：
- DynamoDB 映射表为「飞书↔IDC」唯一真相源
- IDC UserId 为稳定锚点
- 运行态零探测，存量一次性迁移
- AWS 凭证走 IAM Role（无长期 AK/SK）
"""
__version__ = "2.0.0-dev"
