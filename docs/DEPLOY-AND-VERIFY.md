# 真机部署与飞书联调清单 / Local & Feishu Bring-up Checklist

> 从零到端到端跑通的部署与联调清单。按顺序执行，每步带验证点。
> Step-by-step checklist to bring the stack up end-to-end, with a verify point per step.
>
> 端到端验证覆盖五个落点：IDC 用户 / 加组 / Kiro 订阅 / DynamoDB 映射 / 密码邮件。
> 下方「踩坑速查」汇总了部署联调中常见的卡点与解法。

## ⚠️ 踩坑速查（首次联调实录，复用必看）

| 现象 | 根因 | 解法 |
|------|------|------|
| OAuth 报 **20029 重定向 URL 有误** | 飞书后台没加重定向 URL | 安全设置→重定向 URL 加 `http://localhost:8000/api/auth/feishu/callback`（一字不差，http、无尾斜杠） |
| callback 跳转后 **404 Not Found** | BrowserRouter 客户端路由（/auth/callback、/admin）走 StaticFiles 找不到文件 | 已修：main.py SPA fallback（commit e83f234） |
| 申请提交报 **99992351 id not exist** | `ADMIN_OPEN_IDS` 还是占位符 | 登录后从 /api/auth/me 取真实 ou_ open_id 回填 .env 重启 |
| 点卡片按钮 **无权操作 / code 200873**，后端零回调 | 事件/回调订阅方式指向废弃的开发者服务器 API Gateway | 「事件配置」**和**「回调配置」**两个都**切「使用长连接接收回调」 |
| 长连接收到事件但 **卡片回调仍 processor not found** | 订阅的是旧版 `card.action.trigger_v1`，与 SDK `register_p2_card_action_trigger`（新版）结构不匹配 | 回调里删旧版、添加新版 `card.action.trigger`，重新发版 |
| 卡片回调到达但 **do_without_validation 报错** | lark-oapi 1.4.15 该方法名无前导下划线 | 已修：feishu_ws.py（commit e83f234） |
| `uvicorn[standard]` 装不上（watchfiles not found） | 平台/源问题 | 装基础版 `uvicorn==0.34.0` 即可，联调不需要热重载 |

---

## 阶段 0 — 前置准备

- [ ] 一个启用了 IAM Identity Center 的 AWS 账号，记录 `Identity Store ID`、`SSO Instance ARN`、`KIRO_SIGN_IN_URL`
- [ ] 一个具备运行环境的载体（EC2 / ECS / EKS，或本地 Docker 做联调）
- [ ] 飞书企业自建应用（下面阶段 2 创建）

---

## 阶段 1 — AWS 资源

### 1.1 建 DynamoDB 表
```bash
python infra/create_table.py        # 凭证走 aws sso / Role
```
- [ ] 验证：控制台能看到 `kiro-account-mapping`（含 GSI `feishu_open_id-index`）
- [ ] 手动建第二张表 `kiro-account-mapping-requests`（PK=request_id）。可仿照 create_table.py 加一段，或控制台建

### 1.2 IAM Role + 权限
- [ ] 给运行载体绑定 IAM Role（EC2 Instance Profile / ECS Task Role / EKS IRSA）
- [ ] 附加 `infra/iam-policy.json`
- [ ] 验证：在载体上 `aws sts get-caller-identity` 返回的是 Role，不是长期 AK/SK
- [ ] 验证：`aws identitystore list-groups --identity-store-id <ID>` 能列出组（含申请表单要用到的目标组）

> ⚠️ 安全基线：全程**不要**在 `.env` 里填 `AWS_ACCESS_KEY_ID/SECRET`。
>
> ⚠️ **开通链路权限**：订阅步 `q:CreateAssignment` 由 q 服务代理级联调用 sso/sso-directory/identitystore/user-subscriptions，逐个补 action 抠不全，`infra/iam-policy.json` 已按 namespace 放宽这组权限。
> **Provisioning perms**: the `q:CreateAssignment` step is proxied by the q service and cascades into sso/sso-directory/identitystore/user-subscriptions; a tight least-privilege set is un-enumerable, so `infra/iam-policy.json` broadens these by namespace.

### 1.3 Overages 超额上限（可选）

管理页「账号总览」会展示 Kiro Overages 超额上限（Service Quotas: `kiro` / `L-75434B0B`，
"Maximum allowed overage per Kiro profile"，USD/订阅）与最坏月超额敞口测算，
并支持应用内直接发起调高（增量校验 + 二次确认 + 落 requests 表审计）。

- [ ] 权限：`infra/iam-policy.json` 的 Sid `KiroOverageQuotaManage`（Get/GetDefault/RequestIncrease/ListChangeHistory 四个 action）。缺权限不报错，卡片降级显示 —
- [ ] 该 quota 在 **us-east-1**；Overages 默认关闭，需在 Kiro console → Settings 手动开启后 cap 才实际生效

> ⚠️ **上限只可调高，不可调低**（实测 `RequestServiceQuotaIncrease` 对小于当前值的请求直接拒绝
> `IllegalArgumentException`）。调低需开 AWS Support case。应用内调高有两道护栏：
> 单次最多调至当前值 2 倍 + 已有审批中请求时禁止重复提交；每次调高记入 requests 表（谁/何时/从多少到多少）。
> **Overage cap is increase-only**: requests below the current value are rejected by the API;
> lowering it requires an AWS Support case. In-app raises are capped at 2x per request and audited.

---

## 阶段 2 — 飞书应用

### 2.1 创建应用
- [ ] [飞书开放平台](https://open.feishu.cn/) → 创建企业自建应用
- [ ] 记录 `App ID` / `App Secret`

### 2.2 添加能力
- [ ] 添加「网页应用」能力（用于 OAuth 登录）
- [ ] 添加「机器人」能力（用于发/更新审批卡片）

### 2.3 配置 OAuth
- [ ] 安全设置 → 重定向 URL 填：`{部署域名}/api/auth/feishu/callback`
  - 本地联调可用 `http://localhost:8000/api/auth/feishu/callback`

### 2.4 申请权限
- [ ] `contact:user.base:readonly`（OAuth 获取用户基本信息）
- [ ] `im:message:send`（发审批卡片）
- [ ] `im:message`（更新卡片状态）

### 2.5 事件订阅（长连接模式 —— v2 的关键，免公网）
- [ ] 事件与回调 → 订阅方式选 **使用长连接接收回调**
- [ ] 添加事件 `card.action.trigger`（卡片交互回调）

### 2.6 发布
- [ ] 版本管理与发布 → 创建版本 → 提交 → 管理后台审核通过
- [ ] 验证：应用状态为「已发布/已启用」

> ⚠️ 每次改权限/能力后都要**重新创建版本并发布**才生效。

---

## 阶段 3 — 部署后端

### 3.1 配置 .env
```bash
cd backend && cp .env.example .env
```
填入（**无 AK/SK**）：
- [ ] `IDENTITY_STORE_ID` / `SSO_INSTANCE_ARN` / `KIRO_SIGN_IN_URL`
- [ ] `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- [ ] `FEISHU_REDIRECT_URI`（与 2.3 一致）
- [ ] `ADMIN_OPEN_IDS`（你自己的飞书 open_id，先填自己便于联调）
- [ ] `JWT_SECRET`（随机 32+ 字符串）
- [ ] `FRONTEND_URL`（本地 `http://localhost:8000`，前端已 build 进 static 时同源）

> 取自己的 open_id：先随便配一个，登录后看 `/api/auth/me` 返回的 `open_id`，再回填 `ADMIN_OPEN_IDS` 重启。

### 3.2 构建前端 + 启动
```bash
cd frontend && npm install && npm run build   # 产物进 backend/static
cd ../backend && pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
- [ ] 验证：日志出现「飞书 WebSocket 长连接客户端已启动」
- [ ] 验证：`curl localhost:8000/api/health` → `{"status":"ok"}`
- [ ] 验证：浏览器开 `http://localhost:8000/` 看到登录页

---

## 阶段 4 — 端到端联调（核心验证）

### 4.1 登录
- [ ] 点「飞书登录」→ 跳飞书授权 → 回调 → 进 Dashboard
- [ ] 验证：右上角显示你的飞书姓名
- [ ] 验证：`/api/auth/me` 的 `is_admin=true`（已回填自己 open_id）

### 4.2 申请（普通用户视角）
- [ ] 点「申请账号」→ 表单（用户名已自动推荐）→ 选分组/套餐（pro / pro+ / **pro max ($100)** / power）→ 提交
- [ ] 验证：飞书收到一张**审批卡片**（你既是申请人又是管理员，能收到）
- [ ] 验证：「我的申请记录」出现一条 `pending`

### 4.3 审批（飞书端 —— v2 重点）
- [ ] 在飞书卡片上点「✅ 通过」
- [ ] 验证：卡片秒变「⏳ 处理中」（先 ACK）
- [ ] 验证：几秒后卡片变「✅ 已通过」，且收到结果通知卡片
- [ ] 验证：IDC 控制台出现新用户、加了组、有 Kiro 订阅
- [ ] 验证：DynamoDB `kiro-account-mapping` 出现映射（PK=新 UserId，account_role=primary）
- [ ] 验证：邮箱收到密码设置邮件

### 4.4 审批（Web 端 —— 同源验证）
- [ ] 再申请一个副账号 → 进「审批后台」→ 点「通过」
- [ ] 验证：执行成功，且该账号 `account_role=secondary`（一人第二个号自动判副）

### 4.5 防重验证
- [ ] 申请一条 → 飞书点通过的同时，Web 后台也点通过
- [ ] 验证：只执行一次，第二次提示「已处理」（不重复开通）

---

## 阶段 5 — 常见排错

| 现象 | 排查 |
|------|------|
| 卡片点了没反应 | 看日志 `app.feishu_ws`；确认 `card.action.trigger` 已订阅、应用已发布、长连接已启动 |
| OAuth 回调 400 | `FEISHU_REDIRECT_URI` 与飞书后台配置不一致 |
| 开通失败「组不存在」 | `group` 选的组在 IDC 没有，先建组或改选 |
| 开通失败「无法解析凭证」 | 载体没绑 IAM Role，或本地没 `aws sso login` |
| 邮箱已占用 | 该 email 已被某账号用（IDC 邮箱唯一），换邮箱 |
| me 里 is_admin=false | `ADMIN_OPEN_IDS` 没填你的 open_id，回填后重启 |

---

## 联调通过后的下一步

- M3：跑存量迁移脚本（`MigrationResolver`）把历史账号入库
- M4：副账号回收清单 + 对接 Kiro Analytics Dashboard
- 生产化：requests 表加 `user_open_id-index` GSI、state 防 CSRF 收紧、前端 chunk 拆分
