# Production Deployment — EC2 (中英对照 / Bilingual)

> 生产环境的 step-by-step 部署与更新手册。基于 systemd 常驻 + SSM 远程运维 + GitHub Deploy Key。
> Step-by-step guide for deploying & updating the production EC2. Based on systemd + SSM remote ops + GitHub Deploy Key.
>
> 不含反向代理 / 公网入口的搭建（按各自环境处理）。
> Reverse-proxy / public-ingress setup is out of scope here (handle per environment).

---

## 0. 生产环境清单 / Production Inventory

| 项 / Item | 值 / Value |
|------|------|
| EC2 实例 / Instance | `i-030594826594c2089` (t3.small, AL2023, us-east-1) |
| 部署路径 / Deploy path | `/home/ec2-user/kiro-feishu-account-bot` |
| systemd 服务 / Service | `kiro-v2` (User=ec2-user, Restart=always) |
| 运行入口 / Entrypoint | `backend/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| 代码仓库 / Repo | `git@github.com:Sm1-z/kiro-feishu-account-bot.git` (private), branch `main` |
| IAM Role | `kiro-v2-ec2-role` (managed policy `kiro-v2-minimal-policy`) |
| `.env` 来源 / source | SSM Parameter Store `/kiro-v2/env` (SecureString, KMS) |
| 运维通道 / Ops channel | SSM `send-command`（纯 API，无需 SSH / no SSH needed） |
| 健康检查 / Health | `GET /api/health` → `{"status":"ok"}` |

---

## 1. 运维通道 — 为什么用 SSM / Ops Channel — Why SSM

**中文**：本机出口 IP 浮动（15.248.x / 54.222.x 轮换），SSH 锁 IP 不可靠；安全组只开 22、不开 80/443。因此所有远程运维走 **SSM `send-command`**（纯 AWS API，无需 session-manager-plugin、无需 SSH）。

**EN**: The operator's egress IP rotates (15.248.x / 54.222.x), so SSH IP-allowlisting is unreliable; the security group exposes only 22, not 80/443. All remote ops therefore go through **SSM `send-command`** (pure AWS API — no session-manager-plugin, no SSH).

### ⚠️ SSM 执行的三个坑 / Three SSM gotchas

| 坑 / Gotcha | 解法 / Fix |
|------|------|
| 多行脚本直接传会 `syntax error` / Multi-line scripts break with `syntax error` | base64 编码再传：`echo <b64> \| base64 -d \| bash` / base64-encode then pipe |
| SSM 以 root 跑，git 报 `dubious ownership` / SSM runs as root; git complains | 先 `git config --global --add safe.directory <path>`，或脚本用 `sudo -u ec2-user bash` / set safe.directory or run as ec2-user |
| 应用文件属 ec2-user / App files owned by ec2-user | 涉及代码/venv 的命令用 `sudo -u ec2-user bash` / run as ec2-user |

通用调用模板 / Generic invocation template:
```bash
B64=$(base64 < /tmp/script.sh | tr -d '\n')
aws ssm send-command --region us-east-1 --profile default \
  --instance-ids i-030594826594c2089 --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"echo $B64 | base64 -d | sudo -u ec2-user bash\"]"
# 取结果 / fetch result：
aws ssm get-command-invocation --region us-east-1 --profile default \
  --command-id <ID> --instance-id i-030594826594c2089 \
  --query 'StandardOutputContent' --output text
```

---

## 2. 首次部署 / First-time Deploy

> 仅在全新实例上需要。已部署实例日常更新请跳到 §3。
> Only for a fresh instance. For routine updates on an existing instance, skip to §3.

### 2.1 实例与 Role / Instance & Role
- [ ] EC2 (AL2023) 绑定 instance profile `kiro-v2-ec2-role`；该 role 附加 `AmazonSSMManagedInstanceCore`（SSM 必需）+ `kiro-v2-minimal-policy`。
      EC2 with instance profile `kiro-v2-ec2-role`; attach `AmazonSSMManagedInstanceCore` (required for SSM) + `kiro-v2-minimal-policy`.
- [ ] 安装运行时 / Install runtimes：`python3.12`、`nodejs 18`、`git`。
- [ ] 验证 / Verify：SSM 控制台显示实例 `Online`（`aws ssm describe-instance-information`）。

### 2.2 GitHub Deploy Key（只读拉代码 / read-only pull）
**中文**：仓库私有，EC2 用只读 Deploy Key 拉代码（不放个人 token）。

**EN**: The repo is private; EC2 pulls via a read-only Deploy Key (no personal token on the box).

```bash
# 在 EC2 上生成密钥 / generate key on EC2 (as ec2-user)
ssh-keygen -t ed25519 -N "" -f ~/.ssh/kiro_deploy_ed25519 -C "kiro-v2-ec2-deploy"
cat ~/.ssh/kiro_deploy_ed25519.pub   # 复制公钥 / copy the public key
# SSH config 指定该 key / pin the key for github.com
printf 'Host github.com\n  IdentityFile %s\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' \
  ~/.ssh/kiro_deploy_ed25519 >> ~/.ssh/config
```
- [ ] 把公钥加到仓库 / Add the public key as a repo Deploy Key (read-only)：
      `gh api -X POST repos/Sm1-z/kiro-feishu-account-bot/keys -f title=... -f key="<pubkey>" -F read_only=true`
- [ ] 验证 / Verify：`ssh -T git@github.com` → `Hi Sm1-z/...! You've successfully authenticated`

### 2.3 拉代码 / Clone
```bash
cd /home/ec2-user
git clone git@github.com:Sm1-z/kiro-feishu-account-bot.git
cd kiro-feishu-account-bot && git checkout main
```

### 2.4 AWS 资源 / AWS resources
- [ ] 建 DynamoDB 表 / Create tables：`python infra/create_table.py`（建 `kiro-account-mapping` + `-requests`，含 GSI）。
- [ ] IAM policy 见 §5（开通链路权限是这次最大的坑）/ IAM policy — see §5 (provisioning perms were the biggest pitfall).

### 2.5 配置 .env（从 SSM Parameter Store）/ Configure .env (from SSM Parameter Store)
**中文**：`.env` 不入 git（gitignore），生产用 SSM Parameter Store SecureString 保存，避免密钥落入命令历史。

**EN**: `.env` is gitignored. In production it lives in an SSM Parameter Store SecureString, so secrets never hit shell history.

```bash
# 拉取到 backend/.env / pull into backend/.env
aws ssm get-parameter --name /kiro-v2/env --with-decryption --region us-east-1 \
  --query Parameter.Value --output text > /home/ec2-user/kiro-feishu-account-bot/backend/.env
chmod 600 /home/ec2-user/kiro-feishu-account-bot/backend/.env
```
`.env` 关键项 / key fields（**无 AK/SK** / no AK/SK）：`IDENTITY_STORE_ID`、`SSO_INSTANCE_ARN`、`KIRO_SIGN_IN_URL`、`FEISHU_APP_ID/SECRET`、`FEISHU_REDIRECT_URI`、`ADMIN_OPEN_IDS`、`JWT_SECRET`、`FRONTEND_URL`、`ATHENA_*`/`GLUE_TABLE_NAME`（用量看板 / usage dashboard）。

### 2.6 构建 + venv / Build + venv
```bash
cd /home/ec2-user/kiro-feishu-account-bot
# 前端产物进 backend/static（vite outDir 已配）/ frontend builds into backend/static
cd frontend && npm install && npm run build
# 后端 venv / backend venv
cd ../backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 2.7 systemd 常驻 / systemd service
`/etc/systemd/system/kiro-v2.service`：
```ini
[Unit]
Description=Kiro account platform backend
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/kiro-feishu-account-bot/backend
ExecStart=/home/ec2-user/kiro-feishu-account-bot/backend/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now kiro-v2
systemctl is-active kiro-v2          # → active
curl -s localhost:8000/api/health    # → {"status":"ok",...}
```
> ⚠️ 用 `.venv/bin/python -m uvicorn` 而非 `.venv/bin/uvicorn`：venv 若被移动过，入口脚本 shebang 会失效，`python -m` 绕过该问题。
> Use `.venv/bin/python -m uvicorn` (not `.venv/bin/uvicorn`): if the venv was ever moved, the entry-script shebang breaks; `python -m` avoids it.

---

## 3. 日常更新 / Routine Update（最常用 / most common）

> 已有代码变更合并到 GitHub `main` 后，把生产更新到最新。
> After changes are merged to GitHub `main`, bring production up to date.

### 3.1 推代码到 GitHub（在本地 / on your machine）
```bash
git add -A && git commit -m "..." && git push origin main
```

### 3.2 EC2 拉取并重启 / Pull on EC2 and restart
经 SSM 执行以下脚本 / run via SSM (§1 template)：
```bash
cd /home/ec2-user/kiro-feishu-account-bot
git fetch origin -q
git reset --hard origin/main          # ⚠️ 见下方说明 / see note below
HEAD=$(git rev-parse --short HEAD); echo "now at $HEAD"

# 仅当本次改动含前端时才需重建 / rebuild ONLY if frontend changed
# (cd frontend && npm install && npm run build)

sudo systemctl restart kiro-v2
sleep 4
systemctl is-active kiro-v2
curl -s localhost:8000/api/health
```

> ⚠️ **必须用 `git reset --hard origin/main`，不能用 `git pull`**。
> 该仓库历史被 `git filter-repo` 重写过（去客户化清洗），EC2 本地历史与远程已分叉，普通 `pull` 会因历史分叉失败。
> **Must use `git reset --hard origin/main`, not `git pull`.** The repo history was rewritten via `git filter-repo` (de-customization cleanup); the EC2 local history diverged from remote, so a plain `pull` fails on divergent history.

### 3.3 什么时候要 rebuild / When to rebuild
| 改动类型 / Change | 操作 / Action |
|------|------|
| 仅后端 `.py` / backend only | `reset --hard` + 重启 / restart |
| 含 `frontend/**` / frontend touched | 额外 `npm run build`（产物进 `backend/static`）/ also rebuild |
| 改了 `.env` 内容 / `.env` changed | 先更新 SSM `/kiro-v2/env`，再 §2.5 重新拉到 `backend/.env` / update SSM param then re-pull |
| 只改 IAM policy / IAM only | §5（线上 policy 单独改，不走 git pull）/ apply online (§5), no git pull needed |

> 注意 / Note：`backend/static`（前端产物）和 `backend/.env` 都 **gitignored**，`git pull/reset` 不会更新它们——前端改动必须重新 `npm run build`，`.env` 改动必须重新从 SSM 拉。
> Both `backend/static` (frontend build) and `backend/.env` are gitignored; `git pull/reset` won't touch them — rebuild for frontend changes, re-pull from SSM for `.env` changes.

---

## 4. 验证 / Verify（每次更新后 / after each update）

- [ ] `systemctl is-active kiro-v2` → `active`
- [ ] `journalctl -u kiro-v2 -n 5` 出现 / shows「飞书 WebSocket 长连接客户端已启动」+「Application startup complete」
- [ ] `curl -s localhost:8000/api/health` → `{"status":"ok"}`
- [ ] （含前端 / if frontend）线上资源是新产物 / served bundle is the new build：`curl -s <host>/ | grep -o 'assets/[^"]*\.js'`
- [ ] 真机开通冒烟 / Provisioning smoke test（见 §6 / see §6）

---

## 5. IAM 权限 — 开通链路（本次最大的坑）/ IAM — Provisioning Chain (the biggest pitfall)

**中文**：账号开通的订阅步调用 `q:CreateAssignment`（Q Developer 内部 API）。该调用由 **q 服务代理**（CloudTrail `invokedBy=q.amazonaws.com`）**级联**调用 `sso` / `sso-directory` / `identitystore` / `user-subscriptions` 等多个 action。这些级联鉴权**不进 CloudTrail 管理事件**，逐个补 action 会反复漏（曾踩 `sso:DescribeApplication`→`DescribeInstance`→… 多轮仍失败）。最终方案：按 **namespace 放宽**（实测最小权限抠不全，且 claim 不能由 IAM principal 直接建——只能由 q 服务代建）。

**EN**: The subscription step calls `q:CreateAssignment` (an internal Q Developer API). That call is **proxied by the q service** (CloudTrail `invokedBy=q.amazonaws.com`) which **cascades** into multiple `sso` / `sso-directory` / `identitystore` / `user-subscriptions` actions. Those cascaded authz checks **don't appear in CloudTrail management events**, so patching action-by-action keeps missing one (we burned several rounds: `sso:DescribeApplication`→`DescribeInstance`→…). Final approach: **broaden by namespace** (a tight least-privilege set proved un-enumerable, and claims cannot be created directly by an IAM principal — only the q service can).

生效策略 / Effective policy = `infra/iam-policy.json`，关键语句 / key statement `KiroProvisioningServices`：
```json
{ "Sid": "KiroProvisioningServices", "Effect": "Allow",
  "Action": ["identitystore:*","sso:*","sso-directory:*","q:*","user-subscriptions:*","codewhisperer:*"],
  "Resource": "*" }
```

### 更新线上 policy / Update the live policy
```bash
POLICY_ARN=arn:aws:iam::741448945882:policy/kiro-v2-minimal-policy
# 版本上限 5，满了先删最旧的非默认版 / max 5 versions; delete oldest non-default first
OLD=$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
      --query 'Versions[?!IsDefaultVersion]|[-1].VersionId' --output text --profile default)
aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$OLD" --profile default
aws iam create-policy-version --policy-arn "$POLICY_ARN" \
  --policy-document file://infra/iam-policy.json --set-as-default --profile default
```
> IAM 权限实时生效，无需重启服务（role 是调用时鉴权）。
> IAM changes take effect immediately; no restart needed (role is evaluated at call time).

### 排查权限类报错的通法 / How to debug an authz failure
1. **看错误来源 / Identify the failing call**：CloudTrail `lookup-events`，关注 `errorCode=AccessDenied` 且 `invokedBy` 字段——`invokedBy=q.amazonaws.com` 表示是 q 服务代发的级联调用。
2. **区分"权限缺失 vs 身份白名单" / Permission gap vs identity allowlist**：临时给 role 挂 `AdministratorAccess` 实测——若成功＝纯权限问题（按 namespace 补即可）；若仍失败＝服务端身份限制或 SCP（需换思路）。**测完立即卸载 Admin**。
3. 本账号不在 Organization、role 无 permission boundary，故排除 SCP/boundary 干扰。

---

## 6. 真机开通冒烟测试 / Provisioning Smoke Test

经 SSM 在 EC2 上用 EC2 role 跑（最贴近真实）/ Run on EC2 via SSM with the EC2 role (closest to real)：
```python
# backend/.venv/bin/python
from app import provisioner as P
r = P.provision(username="smoke-test", email="smoke@example.com",
                given_name="Smoke", family_name="Test",
                tier="pro max", group_name="Kiro-Test-Group")
print(r.success, r.steps_succeeded, r.error)
# 成功应为 / expect: True ['user_created','group_added','password_email_sent','subscription_created']
import time; time.sleep(5)
print(P.query_tier("smoke-test"))   # → 'pro max'（claim 异步生成，需等几秒 / claim is async, wait a few s）
# 清理 / cleanup（退订 + 删 IDC 用户 + 删映射 / cancel + delete IDC user + delete mapping）
import boto3
idc = boto3.client("identitystore", region_name="us-east-1")
uid = next(u["UserId"] for u in idc.list_users(IdentityStoreId="d-906634fffc", MaxResults=50)["Users"]
           if u["UserName"]=="smoke-test")
print(P.bulk_cancel([uid]))
```
> ⚠️ `query_tier` 返回 `None` 多半是 claim 还没异步生成，等 ~5s 再查；不是失败。
> A `None` from `query_tier` usually means the claim hasn't been created asynchronously yet — wait ~5s and re-query; it's not a failure.

---

## 7. 套餐 / Subscription Tiers

| tier (申请值 / input) | 显示 / Display | `type.amazonQ` (KIRO_ENTERPRISE_*) |
|------|------|------|
| `pro` | Kiro Pro ($20) | `KIRO_ENTERPRISE_PRO` |
| `pro+` | Kiro Pro+ ($40) | `KIRO_ENTERPRISE_PRO_PLUS` |
| `pro max` | Kiro Pro Max ($100) | `KIRO_ENTERPRISE_PRO_MAX` |
| `power` | Kiro Power ($200) | `KIRO_ENTERPRISE_POWER` |

> 开通走 `q:CreateAssignment`（`subscriptionType=Q_DEVELOPER_STANDALONE_*`）；订阅读回 / read-back（`ListUserSubscriptions`、`query_tier`）为 `KIRO_ENTERPRISE_*`。两套命名 `TIER_REVERSE` 双向映射。
> Provisioning uses `q:CreateAssignment` (`subscriptionType=Q_DEVELOPER_STANDALONE_*`); read-back is `KIRO_ENTERPRISE_*`. `TIER_REVERSE` maps both.

---

## 8. 回滚 / Rollback

```bash
# 代码回滚 / code rollback (via SSM)
cd /home/ec2-user/kiro-feishu-account-bot
git reset --hard <good-commit-sha>
sudo systemctl restart kiro-v2

# IAM policy 回滚 / IAM rollback：把旧版本设回默认 / set an older version as default
aws iam set-default-policy-version --policy-arn <ARN> --version-id <vN> --profile default
```

---

## 9. 常见运维命令 / Common Ops Commands（经 SSM / via SSM）

| 目的 / Purpose | 命令 / Command |
|------|------|
| 看服务状态 / Service status | `systemctl is-active kiro-v2` |
| 看日志 / Logs | `journalctl -u kiro-v2 -n 50 --no-pager` |
| 重启 / Restart | `sudo systemctl restart kiro-v2` |
| 当前代码版本 / Current rev | `git -C /home/ec2-user/kiro-feishu-account-bot rev-parse --short HEAD` |
| 健康 / Health | `curl -s localhost:8000/api/health` |
| 实例在线 / Instance online | `aws ssm describe-instance-information --filters Key=InstanceIds,Values=i-030594826594c2089` |
