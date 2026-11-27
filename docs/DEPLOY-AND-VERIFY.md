# 部署与联调清单（积分池版）/ Deploy & Bring-up Checklist (Credit Pool)

> 从零到端到端跑通的部署与联调清单，按顺序执行，每步带验证点。
> 端到端验证覆盖：飞书登录 / 卡片审批 / IDC 用户与加组 / 加入积分池与占座 / 用量上限 / 停用与回收 / 积分池总览 / 用量报表。
> 本清单在 2026-11 用一个后付（POSTPAID）账号完整走通过。

## ⚠️ 踩坑速查（复用必看）

| 现象 | 根因 | 解法 |
|------|------|------|
| 表 / 资源建到了别的 Region | shell 里的 `AWS_REGION` 环境变量**优先于** `.env` | 启动脚本里显式 `export AWS_REGION=<Kiro profile 所在 Region>` |
| 飞书卡片点「通过」提示「目标服务器超时未响应」 | 飞书回调要在约 3 秒内响应 | 已修：回调立即回 toast，开通在后台执行；本地用 credential_process 类凭证（如 SSO 工具）时每建一个 AWS 会话都要几秒，启动时先取一次临时凭证放进进程环境 |
| 飞书后台保存「长连接」模式时报未连接 | 飞书要求长连接客户端已在线才能保存 | 先把后端起起来（日志出现 `connected to wss://`），再去保存订阅方式 |
| 卡片回调到了但报 processor not found | 订阅的是旧版 `card.action.trigger_v1` | 回调订阅只加新版 `card.action.trigger` |
| 开通失败「预付积分池还没有购买席位」 | 预付账号首次买席位前，加入积分池会被拒 | 先在 Kiro 控制台 Credit sharing →「Add seat」买席位 |
| 开通后显示「等待席位」 | 预付池没有空余席位 | 控制台买席位后，到「账号总览」点「分配席位」 |
| 开通失败「已达到 profile 可接入用户数上限」 | Service Quotas `L-295A3FFB` | 在 Service Quotas 控制台（us-east-1）申请提额 |
| 设上限报「超过了积分池当前的 credits 总量」 | 上限不能超过池子当前总量（月度 + 追加） | 降低上限，或先加席位 / 买追加 credits |
| 启动后所有接口 503「未配置 JWT_SECRET」 | `JWT_SECRET` 为空或少于 32 字符（安全基线，fail closed） | 配一个随机长字符串 |
| OAuth 报 **20029 重定向 URL 有误** | 飞书后台没加重定向 URL | 安全设置 → 重定向 URL 加 `{域名}/api/auth/feishu/callback`（本地 `http://localhost:8000/api/auth/feishu/callback`） |
| 登录回调 400「登录状态校验失败」 | 回调里的 state 与浏览器 cookie 不一致（防登录 CSRF） | 从登录页重新点「飞书登录」；不要直接复用旧的回调链接 |
| 申请提交报 **99992351 id not exist** | `ADMIN_OPEN_IDS` 还是占位符 | 登录后从 `/api/auth/me` 取真实 `ou_` open_id 回填 `.env` 重启（open_id 按应用区分，换应用要重取） |
| 用量列一直是 — | 用量报表未配置，或当天还没有报表 | 见 1.4；报表每天 02:00 UTC 写入，只包含前一天用过 Kiro 的人 |
| EC2 上 `pip install` 失败，或启动报 `TypeError: unsupported operand type(s) for \|` | Amazon Linux 2023 自带的 `python3` 是 3.9，代码需要 3.10+ | 装 `python3.12`（`infra/ec2/user-data.sh` 已处理） |
| 卡片回调时好时坏，部分点击没反应 | 同一个飞书应用同时有两个 bot 在线（例如本地 + 云上），回调会随机分给其中一个 | 同一时间只保留一个实例在线 |
| EC2 上服务起不来，日志里取 .env 报 AccessDenied | 实例角色缺 `ssm:GetParameter` / `kms:Decrypt` | 附加 `infra/iam-policy-ssm-env.json`（替换 REGION / ACCOUNT_ID） |

---

## 阶段 0 — 前置准备

- [ ] 一个启用了 IAM Identity Center 的 AWS 账号，记录 `Identity Store ID`、`SSO Instance ARN`、访问门户 URL（`KIRO_SIGN_IN_URL`）
- [ ] 该账号的 Kiro profile 已开启积分池（2026-11 之后新建的 profile 默认就是）
- [ ] 确认计费方式：后付（加人即占座）还是预付（须先买席位）。部署后「积分池」页顶部会显示；预付账号先在控制台买第一批席位
- [ ] 一个可绑 IAM Role 的运行载体（EC2 / ECS / EKS；本地联调用 `aws sso login` 或 profile）
- [ ] 飞书企业自建应用（阶段 2 创建）

---

## 阶段 1 — AWS 资源

### 1.1 DynamoDB 表
```bash
export AWS_REGION=us-east-1          # 改成 Kiro profile 所在 Region
python infra/create_table.py         # 建 kiro-account-mapping 与 kiro-account-mapping-requests（按量计费）
```
- [ ] 验证：两张表都在目标 Region，状态 ACTIVE

### 1.2 IAM Role
- [ ] 给运行载体绑定 IAM Role，附加 `infra/iam-policy.json`
- [ ] 验证：载体上 `aws sts get-caller-identity` 返回的是 Role，不是长期 AK/SK

> ⚠️ 积分池接口（`q:CreateAssignment` / `q:AssignSeats` / `q:SetUsageLimit` 等）会由 Kiro 控制面用调用者的身份继续调用 user-subscriptions / IAM Identity Center，
> 逐个补 action 很难补全，策略里按 namespace 放宽了这组权限。

### 1.3 积分池配置项（可选，留空自动解析）
- `KIRO_ACCOUNT_ID`：留空用 STS 解析
- `KIRO_POOL_ARN`：留空用 ListProfiles 的 `creditPools[0].arn` 自动发现
- `KIRO_CONSOLE_URL`：Kiro 控制台 Credit sharing 页面地址，填了「积分池」页会给跳转链接

### 1.4 用量报表（可选，账号总览的「本月已用」依赖它）

1. 建报表桶（与 Kiro profile 同账号、同 Region），四项 Block Public Access 全开、默认加密：
   ```bash
   B=kiro-user-activity-<ACCOUNT_ID>
   aws s3api create-bucket --bucket $B
   aws s3api put-public-access-block --bucket $B --public-access-block-configuration \
     BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   aws s3api put-bucket-encryption --bucket $B --server-side-encryption-configuration \
     '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
   # 桶策略：infra/user-activity-report/bucket-policy.json 里替换 ACCOUNT_ID / REGION
   aws s3api put-bucket-policy --bucket $B --policy file://bucket-policy.json
   ```
2. 建 Athena 查询结果桶 `kiro-user-report-dashboard-athena-results-<ACCOUNT_ID>`（同样私有加密，建议 7 天过期）
3. 建库建表：Athena 执行 `CREATE DATABASE IF NOT EXISTS kiro_user_report`，再执行
   `infra/user-activity-report/create_table.sql`（替换 ACCOUNT_ID / REGION；分区投影，不需要 Glue crawler）
4. Kiro 控制台 → **Controls** → 选策略 → **Edit** → **Rules and settings** → **Privacy and telemetry** →
   **Kiro user activity reports** → **CSV report** → **Turn on**，**CSV destination** 填 `s3://kiro-user-activity-<ACCOUNT_ID>/reports/`
   → **Save to draft** → **Publish**（profile 还没迁到 Controls 的，在 **Settings → Kiro user activity reports** 里开）
5. `.env` 填 `ATHENA_DATABASE=kiro_user_report`、`ATHENA_OUTPUT_BUCKET=s3://kiro-user-report-dashboard-athena-results-<ACCOUNT_ID>/`、`GLUE_TABLE_NAME=user_report`
- [ ] 验证：次日 02:00 UTC 之后 `s3://kiro-user-activity-<ACCOUNT_ID>/reports/AWSLogs/<ACCOUNT_ID>/KiroLogs/user_report/` 下出现 CSV
  （只包含前一天用过 Kiro 的人）

---

## 阶段 2 — 飞书应用

### 2.1 创建应用
- [ ] [飞书开放平台](https://open.feishu.cn/) → 创建企业自建应用 → 记录 `App ID` / `App Secret`

### 2.2 添加能力
- [ ] 「机器人」能力（发 / 更新审批卡片）
- [ ] 「网页应用」能力：桌面端主页填平台地址（本地 `http://localhost:8000/`），建议选「在浏览器新标签页打开」

### 2.3 安全设置
- [ ] 重定向 URL：`{平台地址}/api/auth/feishu/callback`

### 2.4 权限（「批量导入 / 导出权限」粘贴即可）
```json
{"scopes":{"tenant":["im:message","im:message:send_as_bot"],"user":["contact:user.base:readonly"]}}
```

### 2.5 事件与回调（长连接，免公网）
- [ ] **先启动后端**（阶段 3），确认日志出现 `connected to wss://`
- [ ] 事件配置 → 订阅方式选「使用长连接接收事件」→ 验证显示 Connected → 保存
- [ ] 回调配置 → 订阅方式选「使用长连接接收回调」→ 保存 → 添加回调：**只加新版 `card.action.trigger`**

### 2.6 发布
- [ ] 版本管理与发布 → 创建版本 → 发布（可用范围仅限少数成员时免审；否则需企业管理员审核）
- [ ] 建议：机器人设置里关闭「Allow group invites from other bots」（平台用不到群聊）

> ⚠️ 改权限 / 能力后要重新创建版本并发布才生效。

---

## 阶段 3 — 部署后端

### 3.1 配置 .env
```bash
cd backend && cp .env.example .env
```
- [ ] `AWS_REGION`（Kiro profile 所在 Region）/ `IDENTITY_STORE_ID` / `SSO_INSTANCE_ARN` / `KIRO_SIGN_IN_URL` / `KIRO_GROUP_NAME`（默认组）
- [ ] `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_REDIRECT_URI`（与 2.3 一致）
- [ ] `ADMIN_OPEN_IDS`（先填自己，登录后从 `/api/auth/me` 取）
- [ ] `JWT_SECRET`（随机 32+ 字符）/ `FRONTEND_URL`（前端 build 进 static 时与平台地址同源）
- [ ] 用量报表三项（见 1.4，可后补）
- [ ] **不要**在 `.env` 里填 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`

### 3.2 构建前端 + 启动
```bash
cd frontend && npm ci && npm run build          # 产物进 backend/static
cd ../backend && pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000   # 只监听本机
```
> 对外访问：在同一台机器上用 HTTPS 反向代理（nginx / Caddy）或前置 ALB 把 443 转到 127.0.0.1:8000，
> 安全组只放行公司出口 IP / VPN 网段到 443；不要把 8000 端口直接对 0.0.0.0/0 开放，也不要用 `--host 0.0.0.0`。

- [ ] 验证：日志出现 `connected to wss://msg-frontier.feishu.cn`
- [ ] 验证：`curl 127.0.0.1:8000/api/health` → `{"status":"ok"}`
- [ ] 验证：浏览器打开平台地址看到登录页

### 3.3 云上部署（EC2，不开公网入口）

已在 Amazon Linux 2023 / t4g.small 上实测。安全组不开任何入站端口；飞书回调走长连接（出站），测试访问走 SSM 端口转发，正式对外再加 HTTPS 入口（见 3.4）。

1. **`.env` 放进 SSM SecureString**（不要写进 user-data，user-data 是明文可读的）：
   ```bash
   aws ssm put-parameter --name /kiro-pool-bot/env --type SecureString --value file://.env
   ```
2. **实例角色**：信任 `ec2.amazonaws.com`；附加 `infra/iam-policy.json`（运行权限）、`infra/iam-policy-ssm-env.json`（读上面的参数，替换 REGION / ACCOUNT_ID）、
   托管策略 `AmazonSSMManagedInstanceCore`（SSM 运维与端口转发）；建 instance profile
3. **安全组**：不加任何入站规则（出站默认全开：飞书长连接、Kiro / AWS API、拉代码与依赖）
4. **启动实例**：
   ```bash
   aws ec2 run-instances --image-id <AL2023 AMI> --instance-type t4g.small --subnet-id <子网> \
     --security-group-ids <上一步的 SG> --iam-instance-profile Name=<instance profile> \
     --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1 \
     --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":16,"VolumeType":"gp3","Encrypted":true}}]' \
     --user-data file://infra/ec2/user-data.sh
   ```
   - 强制 IMDSv2；磁盘显式加密（有的账号默认不加密）；不配 SSH 密钥，运维只走 SSM
   - `infra/ec2/user-data.sh` 开头 4 个变量改成自己的（仓库地址、分支、Region、参数名）；约 2 分钟装完依赖、构建前端并启动服务
   - 服务定义 `infra/ec2/kiro-bot.service`：专用账号 `kiro` 运行、只监听 127.0.0.1:8000、启动前从 SSM 取 `.env`（600）、挂了自动重启
5. **验证**（SSM Run Command 或 Session Manager）：
   ```bash
   systemctl is-active kiro-bot
   curl -s 127.0.0.1:8000/api/health             # {"status":"ok"}
   ss -ltn | grep :8000                           # 只应看到 127.0.0.1:8000
   journalctl -u kiro-bot | grep 'connected to wss'
   ```
6. **测试访问**：本机开 SSM 端口转发，浏览器打开 `http://localhost:8000`（飞书重定向 URL 可以继续用 localhost 那条）：
   ```bash
   aws ssm start-session --target <实例 ID> --document-name AWS-StartPortForwardingSession \
     --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
   ```
   > 同一个飞书应用只能有一个 bot 在线：云上起来之前先停掉本地 / 其它实例。
7. **更新代码**：
   ```bash
   sudo -u kiro git -C /opt/kiro-bot/app pull
   cd /opt/kiro-bot/app/frontend && sudo -u kiro npm ci && sudo -u kiro npm run build
   cd ../backend && sudo -u kiro .venv/bin/pip install -q -r requirements.txt && sudo systemctl restart kiro-bot
   ```
   改了 `.env` 的话更新 SSM 参数后 `systemctl restart kiro-bot` 即可。

> 参考成本：t4g.small + 16 GB gp3 + 公网 IPv4 约 $15 / 月；不用时可以停机（只收磁盘费）。

### 3.4 正式对外访问（需要客户先做决定）

员工要从自己的浏览器访问时，加一个 HTTPS 入口：ALB + ACM 证书（或同机 nginx / Caddy）把 443 转到实例的 127.0.0.1:8000，
安全组只放行公司出口 IP / VPN 网段到 443；不要开放 8000，也不要用 `--host 0.0.0.0`。
飞书后台的重定向 URL、网页应用主页和 `.env` 里的 `FEISHU_REDIRECT_URI` / `FRONTEND_URL` 同步改成 HTTPS 域名。

---

## 阶段 4 — 端到端联调（核心验证）

### 4.1 登录
- [ ] 点「飞书登录」→ 飞书授权 → 回到「我的 Kiro 账号」
- [ ] 验证：`/api/auth/me` 的 `is_admin=true`（已回填自己 open_id）

### 4.2 积分池总览
- [ ] 管理后台 →「积分池」：计费方式、剩余 credits、席位、配额与控制台一致；没有报错提示条

### 4.3 申请 → 飞书审批 → 自动开通
- [ ] 管理后台 →「分组管理」新建一个测试组
- [ ] 员工端「申请账号」：填测试用户名 / 邮箱、选组、每月上限填 100 → 提交；飞书收到审批卡片（卡片底部有积分池现状）
- [ ] 飞书卡片点「✅ 通过」：**马上**弹出「已受理，正在处理…」；几秒后卡片变「✅ 已通过」，并收到结果通知
- [ ] 验证：IDC 出现新用户并在组里；「账号总览」里该账号「已占座」、本月上限 100（个人上限）；测试邮箱收到密码设置邮件
- [ ] 验证：再点「申请账号」按钮是灰的（每人一个账号）

### 4.4 用量上限
- [ ] 员工端「申请调整上限」→ 飞书通过 → 生效上限变为新值
- [ ] 管理端：profile 默认上限设 150，个人上限设 120 → 生效 120；删除个人上限 → 回落到 150；删除 profile 默认上限 → 无上限

### 4.5 停用 / 恢复 / 回收
- [ ] 「停用」→ 该账号变「不在池中」；「恢复」→ 回到「已占座」
- [ ] 勾选「同时删除 IDC 用户并解除飞书映射」再停用 → IDC 用户消失、账号从列表移除
- [ ] 「申请审批 → 全部」能看到每一步的审计记录

### 4.6 防重
- [ ] 同一条申请：飞书点通过的同时 Web 后台也点通过 → 只执行一次，另一端提示已处理

> 测试成本：后付池每个测试用户占座即计费，按天折算（月底几天约 $2–3 / 人）；移出不退当月费用，测完及时回收，避免下个账期整月计费。

---

## 阶段 5 — 常见排错

| 现象 | 排查 |
|------|------|
| 卡片点了没反应 | 看日志 `app.feishu_ws`；确认回调订阅了新版 `card.action.trigger`、应用已发布、长连接在线 |
| 「积分池」页报「无法读取积分池」 | Kiro profile 没开积分池，或运行角色缺 `q:*` / `user-subscriptions:*` 读权限 |
| 开通失败「组不存在」 | 申请里选的组在 IDC 没有，先在「分组管理」建组 |
| 开通失败「无法解析凭证」 | 载体没绑 IAM Role，或本地凭证过期 |
| 邮箱已占用 | 该 email 已被某账号用（IDC 邮箱唯一），换邮箱 |
| 实况显示「（快照）」 | 拉取积分池实况失败（网络 / 权限），界面回退到映射表快照 |
