#!/bin/bash
# 从 SSM Parameter Store（SecureString）取 .env 写到 backend 目录。由 kiro-bot.service 的 ExecStartPre 调用。
# 需要实例角色有 ssm:GetParameter（该参数）+ kms:Decrypt（经 SSM），见 infra/iam-policy-ssm-env.json。
set -euo pipefail
umask 077
: "${KIRO_ENV_PARAM:?需要在 /etc/kiro-bot.conf 里设置 KIRO_ENV_PARAM}"
: "${AWS_REGION:?需要在 /etc/kiro-bot.conf 里设置 AWS_REGION}"
aws ssm get-parameter --region "$AWS_REGION" --name "$KIRO_ENV_PARAM" --with-decryption \
  --query Parameter.Value --output text > /opt/kiro-bot/app/backend/.env
chown kiro:kiro /opt/kiro-bot/app/backend/.env
