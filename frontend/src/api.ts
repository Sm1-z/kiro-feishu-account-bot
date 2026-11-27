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

// 积分池状态：SEATED 已占座 / AWAITING_SEAT 等待席位 / null 不在积分池
export type PoolStatus = 'SEATED' | 'AWAITING_SEAT' | null
// 生效上限来源：USER 个人 / GROUP 组 / POOL profile 默认 / NONE 无上限 / null 查询失败
export type LimitScope = 'USER' | 'GROUP' | 'POOL' | 'NONE' | null

export interface Account {
  kiro_user_id: string
  kiro_username: string
  kiro_email: string
  team: string
  status: string
  account_role: string
  usage_limit: number | null   // 个人上限快照（平台设置时记录）
  live_synced: boolean
  pool_status: PoolStatus
  limit: number | null         // 生效上限（credits / 月）
  limit_scope: LimitScope
  month_credits: number | null // 本月已用（User Activity Report，日级延迟）
}

// 有效 = 在积分池里（实况优先；控制台直接移出不会回写映射表）
export const isAccountActive = (a: { live_synced: boolean; pool_status: PoolStatus; status: string }) =>
  a.live_synced ? a.pool_status != null : a.status === 'active'

export const LIMIT_SCOPE_LABEL: Record<string, string> = {
  USER: '个人上限', GROUP: '组上限', POOL: 'profile 默认', NONE: '无上限',
}

export interface Me {
  open_id: string
  name: string
  is_admin: boolean
  accounts: Account[]   // 积分池版每人最多一个账号
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
export const requestLimitChange = (data: { kiro_user_id: string; requested_limit: number; reason?: string }) =>
  api.post('/api/requests/limit-change', data)
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
  status: string
  account_role: string
  usage_limit: number | null
  // 积分池实况（live_synced=false 时不可信，回退快照）
  live_synced: boolean
  pool_status: PoolStatus
  pool_direct: boolean | null
  // 生效上限
  limit: number | null
  limit_scope: LimitScope
  limit_used_pct: number | null
  // 用量（User Activity Report；null = 报表不可用）
  usage_messages: number | null
  usage_credits: number | null
  usage_last_active: string | null
  usage_active_days: number | null
  month_credits: number | null
  month_messages: number | null
  month_active_days: number | null
  month_last_active: string | null
}
export const getAccounts = (force = false) =>
  api.get<AccountRow[]>('/api/admin/accounts', { params: force ? { force: true } : {} })
    .then((r) => r.data)

export interface UsageHistory {
  user_id: string
  year: number
  month: number
  source: 'athena' | null
  available: boolean
  has_usage_entry: boolean
  credit_used: number | null
  messages: number | null
  active_days: number | null
  last_active: string | null
}
export const getUsageHistory = (kiro_user_id: string, year: number, month: number) =>
  api.get<UsageHistory>(`/api/admin/accounts/${kiro_user_id}/usage-history`, { params: { year, month } })
    .then((r) => r.data)

// ---- 积分池总览 ----
export interface AddonItem {
  id: string
  quantity: number
  used: number
  left: number
  source: string | null
  purchased_at: string | null
  expires_at: string | null
}
export interface PoolBalance {
  payment_model: 'PREPAID' | 'POSTPAID'
  prepaid: boolean
  cycle_start: string
  cycle_end: string
  days_left: number
  monthly: { quantity: number; used: number; left: number; seat_count: number; credits_per_seat: number }
  addon: { quantity: number; used: number; left: number; expiring_this_cycle: number; items: AddonItem[] }
  total_quantity: number
  total_left: number
  avg_daily_used: number
  rate_is_lower_bound: boolean
  monthly_runout_at: string | null
  pool_runout_at: string | null
  seat_price_usd: number
  seat_fee_full_month_usd: number
  addon_credit_price_usd: number
}
export interface PoolSeats {
  seated: number | null
  awaiting: number | null
  total: number | null       // 后付池为 null
  available: number | null   // 后付池为 null
  pending_removal: number | null
  pending_removal_effective: number | null
}
export interface QuotaItem { code: string; name: string; value: number | null; adjustable: boolean; console_url: string }
export interface PoolInfo {
  errors: string[]
  console_url: string
  context: { payment_model: 'PREPAID' | 'POSTPAID'; prepaid: boolean; region: string; account_id: string } | null
  balance?: PoolBalance | null
  seats?: PoolSeats | null
  profile_limit?: number | null
  purchases?: { id: string; quantity: number; status: string; source: string; created_at: string; expires_at: string }[] | null
  quotas?: { onboarding_cap: QuotaItem | null; monthly_purchase_cap: QuotaItem | null } | null
}
export const getPool = (force = false) =>
  api.get<PoolInfo>('/api/admin/pool', { params: force ? { force: true } : {} }).then((r) => r.data)

export interface LimitItem { scope: 'USER' | 'GROUP' | 'POOL'; scope_id: string; limit: number | null; effective_from: number | null }
export const getLimits = (force = false) =>
  api.get<LimitItem[]>('/api/admin/limits', { params: force ? { force: true } : {} }).then((r) => r.data)
// usage_limit=null 表示删除；0 表示禁止使用
export const setLimit = (scope: 'USER' | 'POOL', scope_id: string, usage_limit: number | null) =>
  api.post('/api/admin/limits', { scope, scope_id, usage_limit })

export const createGroup = (group_name: string) =>
  api.post('/api/admin/groups', { group_name })

// ---- 停用（移出积分池）/ 回收 / 恢复 / 重新分配席位 ----
export interface BatchResult {
  user_id: string
  username: string
  success: boolean
  error: string
  warning?: string
}
export interface BatchResponse {
  recycle?: boolean
  results: BatchResult[]
  succeeded: number
  failed: number
}
export const deactivateAccounts = (kiro_user_ids: string[], recycle = false) =>
  api.post<BatchResponse>('/api/admin/accounts/deactivate', { kiro_user_ids, recycle }).then((r) => r.data)
export const reactivateAccounts = (kiro_user_ids: string[]) =>
  api.post<BatchResponse>('/api/admin/accounts/reactivate', { kiro_user_ids }).then((r) => r.data)
export const retrySeats = (kiro_user_ids: string[]) =>
  api.post<BatchResponse>('/api/admin/accounts/retry-seats', { kiro_user_ids }).then((r) => r.data)

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
  has_account?: boolean  // 每人一个账号：已有账号的不能再绑定
}
export const getUnlinked = () =>
  api.get<UnlinkedAccount[]>('/api/admin/unlinked').then((r) => r.data)
export const getFeishuUsers = () =>
  api.get<FeishuUser[]>('/api/admin/feishu-users').then((r) => r.data)
export const linkUnlinked = (kiro_user_id: string, feishu_open_id: string, feishu_name: string) =>
  api.post('/api/admin/unlinked/link', { kiro_user_id, feishu_open_id, feishu_name })

export default api