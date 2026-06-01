# Kiro 账号管理平台 v2 — 实现

> 设计文档见 [../docs/design/](../docs/design/)。本目录是 v2 的代码实现，与原版 Lambda（仓库根目录）隔离。

## 进度

| 里程碑 | 状态 | 内容 |
|--------|:----:|------|
| M1 安全基线 | ✅ | IAM Role 凭证链（无 AK/SK）、最小权限 Policy |
| M2 核心范式 | ✅ | DynamoDB 映射层、Provisioner、关联解析器 + 单测 23 通过 |
| M3 迁移 | ⬜ | 存量迁移脚本、真名表退役 |
| M4 增值 | ⬜ | 副账号回收、用量打通、成本护栏 |
| Web/审批/前端 | ⬜ | FastAPI 路由、飞书 WS、React（可参考早期方案） |

## 已实现模块

```
backend/app/
├── config.py        配置层（pydantic-settings，仅资源标识，无密钥字段）
├── aws.py           AWS 会话/凭证唯一出口（默认链=Role，无 AK/SK 读取路径）
├── mapping_store.py DynamoDB 映射层（PK=kiro_user_id, GSI=feishu_open_id，1:N，主/副）
├── provisioner.py   开通/升级/取消/查询/批量（幂等 + 429 退避）
└── resolver.py      关联解析器（运行态零探测 + 迁移期 email/拼音兜底）
infra/
├── iam-policy.json  最小权限策略
└── create_table.py  建表脚本
backend/tests/       moto + mock 单测（23 passed）
```

## 核心范式（与早期方案的本质区别）

- **运行态零探测**：日常只读 DynamoDB 映射，不在热路径拼音探测 IDC
- **UserId 锚点**：以 IDC UserId（不随改名变）为主键，真名保护表退役
- **拼音降级**：仅迁移期 `MigrationResolver` 走拼音/email 兜底
- **无密钥**：凭证全程 IAM Role

## 本地开发

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填资源标识，无需填 AK/SK（用 aws sso login）

# 跑测试（用 moto mock，无需真实 AWS）
pip install pytest moto
python -m pytest tests/ -q
```

## 部署前置

1. 建表：`python infra/create_table.py`（凭证走 Role / aws sso）
2. 绑定 IAM Role，附加 `infra/iam-policy.json`
3. 容器形态见设计文档 [01-architecture.md](../docs/design/01-architecture.md#部署形态建议按客户场景)
</content>
