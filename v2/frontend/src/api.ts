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
}

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

export default api
