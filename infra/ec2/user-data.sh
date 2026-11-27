#!/bin/bash
# EC2（Amazon Linux 2023）启动脚本：装依赖 → 拉代码 → 构建前端 → 建 venv → 安装并启动 kiro-bot 服务。
# 本脚本不含任何密钥：.env 放在 SSM SecureString（KIRO_ENV_PARAM），服务启动时再取。
# 用法：改下面 4 个变量，作为 run-instances 的 --user-data 传入。
set -euxo pipefail
REPO_URL="https://github.com/Sm1-z/kiro-feishu-account-bot.git"
BRANCH="main"
AWS_REGION="us-east-1"                 # Kiro profile 所在 Region
KIRO_ENV_PARAM="/kiro-pool-bot/env"    # 存 .env 的 SSM SecureString 参数名

# Amazon Linux 2023 自带的 python3 是 3.9，代码需要 3.10+，单独装 3.12（没有时退回 3.11）
dnf install -y git nodejs npm python3.12 python3.12-pip || dnf install -y git nodejs npm python3.11 python3.11-pip
PY=$(command -v python3.12 || command -v python3.11)

id kiro >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/kiro-bot --shell /sbin/nologin kiro
cd /opt/kiro-bot
[ -d app ] || sudo -u kiro git clone --depth 1 --branch "$BRANCH" "$REPO_URL" app
cd /opt/kiro-bot/app/frontend && sudo -u kiro npm ci --no-audit --no-fund && sudo -u kiro npm run build
cd /opt/kiro-bot/app/backend && sudo -u kiro "$PY" -m venv .venv && sudo -u kiro .venv/bin/pip install -q -r requirements.txt

printf 'AWS_REGION=%s\nKIRO_ENV_PARAM=%s\n' "$AWS_REGION" "$KIRO_ENV_PARAM" > /etc/kiro-bot.conf
install -m 755 /opt/kiro-bot/app/infra/ec2/fetch-env.sh /usr/local/bin/kiro-bot-fetch-env.sh
install -m 644 /opt/kiro-bot/app/infra/ec2/kiro-bot.service /etc/systemd/system/kiro-bot.service
systemctl daemon-reload
systemctl enable --now kiro-bot
