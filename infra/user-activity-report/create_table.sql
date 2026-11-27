-- Kiro User Activity Report（CSV）的 Athena 表：只取固定在最前面的 14 个静态列。
-- 报表后面的模型 / 工具列每天会变（按列名读，位置不固定），这里不建，按位置读前 14 列是稳定的。
-- 用分区投影（partition projection），不需要 Glue crawler，也不需要额外的 IAM 角色。
-- 把 ACCOUNT_ID、REGION 替换成 Kiro profile 所在的账号和 Region，先建库：CREATE DATABASE IF NOT EXISTS kiro_user_report
CREATE EXTERNAL TABLE IF NOT EXISTS kiro_user_report.user_report (
  `date` string, userid string, client_type string, chat_conversations string, credits_used string,
  overage_cap string, overage_credits_used string, overage_enabled string, profileid string,
  subscription_tier string, total_messages string, new_user string, user_email string, usage_limit string)
PARTITIONED BY (year string, month string, day string)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"')
LOCATION 's3://kiro-user-activity-ACCOUNT_ID/reports/AWSLogs/ACCOUNT_ID/KiroLogs/user_report/REGION/'
TBLPROPERTIES (
  'skip.header.line.count' = '1',
  'projection.enabled' = 'true',
  'projection.year.type' = 'integer', 'projection.year.range' = '2026,2035',
  'projection.month.type' = 'integer', 'projection.month.range' = '1,12', 'projection.month.digits' = '2',
  'projection.day.type' = 'integer', 'projection.day.range' = '1,31', 'projection.day.digits' = '2',
  'storage.location.template' = 's3://kiro-user-activity-ACCOUNT_ID/reports/AWSLogs/ACCOUNT_ID/KiroLogs/user_report/REGION/${year}/${month}/${day}/00/')
