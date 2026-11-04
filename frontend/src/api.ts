// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import axios from 'axios'

const api = axios.create({ baseURL: '' })

// 自动带 JWT
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 401 自动回登录
api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      if (location.pathname !== '/login') location.href = '/login'
    }
    return Promise.reject(err)
  }
)

export interface Account {
  kiro_user_id: string
  kiro_username: string
  kiro_email: string
  tier: string
  team: string
  status: string
  account_role: string
  live_synced: boolean
  live_status: string | null
  live_tier: string | null
}

// 有效状态：实况优先（控制台退订不回写映射表，快照会滞后）；CANCELLED 不算有效
export const isAccountActive = (a: Account) =>
  a.live_synced
    ? a.live_status != null && a.live_status !== 'CANCELLED'
    : a.status === 'active'

export interface Me {
  open_id: string
  name: string
  is_admin: boolean
  quota: number
  accounts: Account[]
  suggested_username: string
}

export interface ReqItem {
  request_id: string
  user_open_id: string
  user_name: string
  type: string
  status: string
  payload: any
  result: any
  review_comment?: string
  created_at: number
}

export const getMe = () => api.get<Me>('/api/auth/me').then((r) => r.data)
export const applyAccount = (data: any) => api.post('/api/requests/apply', data)
export const upgradeAccount = (data: any) => api.post('/api/requests/upgrade', data)
export const quotaIncrease = (data: any) => api.post('/api/requests/quota-increase', data)
export const myRequests = () => api.get<ReqItem[]>('/api/requests/mine').then((r) => r.data)
export const getGroups = () => api.get<string[]>('/api/requests/groups').then((r) => r.data)
export const adminRequests = (status?: string) =>
  api.get<ReqItem[]>('/api/admin/requests', { params: { status } }).then((r) => r.data)
export const approve = (id: string) => api.post(`/api/admin/requests/${id}/approve`)
export const reject = (id: string, comment = '') =>
  api.post(`/api/admin/requests/${id}/reject`, { comment })

export interface AccountRow {
  kiro_user_id: string
  feishu_open_id: string
  feishu_name: string
  kiro_username: string
  kiro_email: string
  team: string
  tier: string
  status: string
  account_role: string
  live_synced: boolean
  live_status: string | null
  live_tier: string | null
  usage_messages: number | null
  usage_credits: number | null
  usage_conversations: number | null
  usage_last_active: string | null
  usage_active_days: number | null
  // 费用/额度（计费口径，直读订阅计费服务；null = 查询失败或无订阅记录）
  billing_synced: boolean
  billing_error: string
  billing_plan_code: string | null
  billing_plan_start: string | null
  billing_current_charges: number | null
  billing_currency: string
  billing_account_status: string | null
  billing_pending_plan: string | null
  billing_pending_scheduled: string | null
  billing_credit_limit: number | null
  billing_overage_rate: number | null
  // 本月活动（User Activity Report 本月聚合）；null = 报表不可用
  usage_month_messages: number | null
  usage_month_active_days: number | null
  usage_month_last_active: string | null
  // 本月已用 / 超额 credits：User Activity Report 本月聚合（活动口径，日级）；null = 报表不可用
  billing_credit_used: number | null
  billing_credit_used_source: 'athena_mtd' | null
  billing_overage_credits: number | null
  billing_overage_charges: number | null   // 预估 = 超额 credits × 单价
  // 订阅计费服务返回的用量字段（仅参考）
  billing_reported_credit_used: number | null
  billing_reported_overage_charges: number | null
}
export interface BillingHistory {
  user_id: string
  year: number
  month: number
  source: 'athena' | null
  available: boolean
  has_usage_entry: boolean
  credit_used: number | null
  overage_credits: number | null
  overage_rate: number
  overage_charges_est: number | null
  messages: number | null
  active_days: number | null
  last_active: string | null
  credit_limit: number | null
  plan_code: string | null
}
export const getBillingHistory = (kiro_user_id: string, year: number, month: number) =>
  api.get<BillingHistory>(`/api/admin/accounts/${kiro_user_id}/billing-history`, { params: { year, month } })
    .then((r) => r.data)
export const getAccounts = (force = false) =>
  api.get<AccountRow[]>('/api/admin/accounts', { params: force ? { force: true } : {} })
    .then((r) => r.data)

export interface OverageCap {
  value: number
  quota_name: string
  adjustable: boolean
  region: string
  console_url: string
}
export interface OverageCapPending {
  desired_value: number
  status: string
  requested_at: string
}
export interface OverageCapInfo {
  cap: OverageCap | null
  pending: OverageCapPending | null
}
export const getOverageCap = () =>
  api.get<OverageCapInfo>('/api/admin/overage-cap').then((r) => r.data)
export const raiseOverageCap = (desired_value: number) =>
  api.post('/api/admin/overage-cap', { desired_value })
export const createGroup = (group_name: string) =>
  api.post('/api/admin/groups', { group_name })

// 停用 / 回收（对应 Kiro 控制台「Deactivate Kiro plan」的两个选项）
export type DeletionMode = 'end_of_period' | 'immediate'
export interface DeactivateResult {
  user_id: string
  username: string
  success: boolean
  error: string
}
export interface DeactivateResponse {
  mode: DeletionMode
  recycle: boolean
  results: DeactivateResult[]
  succeeded: number
  failed: number
}
export const deactivateAccounts = (kiro_user_ids: string[], mode: DeletionMode, recycle = false) =>
  api.post<DeactivateResponse>('/api/admin/accounts/deactivate', { kiro_user_ids, mode, recycle })
    .then((r) => r.data)
// 恢复订阅（撤销月底停用）：对 CANCELLED 账号按原套餐重新订阅
export const reactivateAccounts = (kiro_user_ids: string[]) =>
  api.post<DeactivateResponse>('/api/admin/accounts/reactivate', { kiro_user_ids })
    .then((r) => r.data)

export interface UnlinkedAccount {
  kiro_user_id: string
  kiro_username: string
  kiro_email: string
  display_name: string
  suggested_open_id: string
  suggested_name: string
  confidence: '' | 'email' | 'pinyin'
}
export interface FeishuUser {
  open_id: string
  name: string
  email: string
}
export const getUnlinked = () =>
  api.get<UnlinkedAccount[]>('/api/admin/unlinked').then((r) => r.data)
export const getFeishuUsers = () =>
  api.get<FeishuUser[]>('/api/admin/feishu-users').then((r) => r.data)
export const linkUnlinked = (kiro_user_id: string, feishu_open_id: string, feishu_name: string) =>
  api.post('/api/admin/unlinked/link', { kiro_user_id, feishu_open_id, feishu_name })

export default api