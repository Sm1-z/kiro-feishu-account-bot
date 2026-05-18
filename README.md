# Kiro Bot

> **⚠️ 本方案由 AWS SA Yuanhao 设计，仅适用于测试环境，不建议用于生产环境。**

飞书机器人，用于在飞书群内自助管理 Kiro IDE 账号（开通、升级、查询套餐）。

## 功能

| 触发词 | 功能 | 说明 |
|--------|------|------|
| `申请kiro` / `/kiro-apply` | 开通账号 | 填写表单 → 创建 IAM IC 用户 → 加组 → 发密码邮件 → 订阅 Kiro |
| `升级kiro` / `/kiro-upgrade` | 升级套餐 | 输入用户名 + 选目标套餐 → UpdateAssignment |
| `查询kiro` / `/kiro-query` | 查询套餐 | 输入用户名 → 显示当前订阅 |
| `帮助` / `/help` | 帮助 | 显示使用说明 |

## 套餐

| 套餐 | 价格 | Credits |
|------|------|---------|
| Kiro Pro | $20/mo | 1,000 |
| Kiro Pro+ | $40/mo | 2,000 |
| Kiro Power | $200/mo | 10,000 |

## 架构

```
飞书群 @机器人 → API Gateway (HTTP API) → Lambda → AWS Identity Center + Kiro API
```

### 文件结构

```
kiro-bot/
├── lambda_function.py   # Lambda 入口，路由消息/卡片回调
├── feishu.py            # 飞书 API 客户端（签名、token、消息发送）
├── cards.py             # 飞书交互卡片模板
├── permission.py        # 两层权限校验（chat_id + admin open_id）
└── provisioner.py       # 核心业务（开通/升级/查询）
```

### 开通流程（4 步）

1. `identitystore:CreateUser` — 创建 IAM Identity Center 用户
2. `identitystore:CreateGroupMembership` — 加入 SSO 组
3. `SWBUPService.UpdatePassword` — 发送密码设置邮件（SigV4 签名）
4. `AmazonQDeveloperService.CreateAssignment` — 订阅 Kiro 套餐（SigV4 签名）

所有步骤均幂等：用户已存在/已在组内/已订阅不会报错。

## 权限模型

- **chat_id 白名单**：非白名单群的消息静默丢弃
- **admin open_id 白名单**：白名单群内非管理员操作被明确拒绝

## 环境变量

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `FEISHU_APP_ID` | ✅ | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | ✅ | 飞书应用 App Secret |
| `ALLOWED_CHAT_IDS` | ✅ | 允许的群 chat_id（逗号分隔） |
| `ADMIN_OPEN_IDS` | ✅ | 管理员 open_id（逗号分隔） |
| `IDENTITY_STORE_ID` | ✅ | IAM Identity Center Identity Store ID |
| `TARGET_REGION` | | Kiro 所在 region（默认 `us-east-1`） |
| `KIRO_GROUP_NAME` | | SSO 组名（默认 `Q`） |
| `SSO_INSTANCE_ARN` | ✅ | SSO Instance ARN（用于查询订阅） |
| `KIRO_SIGN_IN_URL` | ✅ | SSO 登录地址 |
| `FEISHU_ENCRYPT_KEY` | | 飞书加密事件密钥（启用加密模式时填） |

## IAM 权限

Lambda 执行角色需要：

```json
{
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Effect": "Allow",
      "Action": ["sso:*", "identitystore:*", "sso-directory:*"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["q:*", "codewhisperer:*", "user-subscriptions:*"],
      "Resource": "*"
    }
  ]
}
```
