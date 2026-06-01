import { useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Spin } from 'antd'

export default function AuthCallback() {
  const [params] = useSearchParams()
  const nav = useNavigate()
  useEffect(() => {
    const token = params.get('token')
    if (token) {
      localStorage.setItem('token', token)
      nav('/', { replace: true })
    } else {
      nav('/login', { replace: true })
    }
  }, [])
  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '100vh' }}>
      <Spin size="large" tip="登录中…" />
    </div>
  )
}
