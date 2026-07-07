# Kiro 账号管理平台

> Kiro 账号自助管理：飞书 OAuth 登录 → Web 自助申请 → 飞书卡片审批（免公网 WS 长连接）
> → 自动开通 IDC 用户/加组/发密码邮件/Kiro 订阅 → DynamoDB 映射（UserId 锚点）。
> 产品文档见 [docs/PRODUCT.md](docs/PRODUCT.md)；部署与联调清单见 [docs/DEPLOY-AND-VERIFY.md](docs/DEPLOY-AND-VERIFY.md)；架构图见 [docs/architecture-aws.png](docs/architecture-aws.png)（AWS 图标版，源文件 [docs/architecture-aws.drawio](docs/architecture-aws.drawio)）。

## 核心能力

| 能力 | 说明 |
|------|------|
| 安全基线 | 凭证全程走 IAM Role，无长期 AK/SK；最小权限 Policy |
| 自助申请 | 飞书 OAuth 登录 + Web 表单自助申请账号，自动推荐用户名 |
| 飞书审批 | 卡片式审批（手机端可批），WS 长连接接收回调、免公网域名 |
| 自动开通 | 一键完成 IDC 建用户 / 加组 / 发密码邮件 / Kiro 订阅，全步骤幂等 |
| 账号治理 | UserId 锚点 + 一人多账号（主/副）+ DynamoDB 映射为唯一真相源 |
| 用量看板 | 对接 Kiro Analytics，管理端账号总览按人聚合 + 用量图表 |
| 超额治理 | Overages 上限展示 + 应用内调高（2 倍护栏 + 审计）+ 最坏月超额敞口测算 |

## 已实现模块

```
backend/app/
├── config.py        配置层（pydantic-settings，仅资源标识，无密钥字段）
├── aws.py           AWS 会话/凭证唯一出口（默认链=Role，无 AK/SK 读取路径）
├── mapping_store.py DynamoDB 映射层（PK=kiro_user_id, GSI=feishu_open_id，1:N，主/副）
├── provisioner.py   开通/升级/取消/查询/批量（幂等 + 429 退避）
├── resolver.py      关联解析器（运行态零探测 + 迁移期 email/拼音兜底）
├── request_store.py 申请/审批记录 + 状态机条件更新防重
├── approval.py      审批执行引擎（抢占防重 + 异步执行 + 写映射）
├── quotas.py        Overages 超额上限查询/调高（Service Quotas + TTL 缓存 + 增量护栏）
├── feishu.py        飞书 OAuth + 卡片收发
├── cards.py         飞书卡片模板
├── feishu_ws.py     WS 长连接（免公网，先 ACK 后异步）
├── auth.py          JWT + 认证依赖
├── schemas.py       API 请求模型
├── routers/         auth / request / admin 路由（16 API）
└── main.py          FastAPI 入口（启动 WS + 托管前端）
frontend/            React + TS + AntD（登录/Dashboard/审批面板）
infra/
├── iam-policy.json  最小权限策略
└── create_table.py  建表脚本
backend/tests/       moto + mock 单元测试
```

## 核心范式

- **运行态零探测**：日常只读 DynamoDB 映射，不在热路径拼音探测 IDC
- **UserId 锚点**：以 IDC UserId（不随改名变）为主键，无需维护真名映射表
- **拼音降级**：仅存量迁移期走拼音/email 兜底，运行态纯 DB 读路径
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
3. 部署与端到端联调步骤见 [docs/DEPLOY-AND-VERIFY.md](docs/DEPLOY-AND-VERIFY.md)

## Disclaimer

This is sample code, for non-production usage. You should work with your security
and legal teams to meet your organizational security, regulatory and compliance
requirements before deployment.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
</content>
