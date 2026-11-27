// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Spin } from 'antd'

export default function AuthCallback() {
  const nav = useNavigate()
  useEffect(() => {
    // 后端把 token 放在 URL fragment（#token=…）里：不会发给服务器、不进访问日志和 Referer
    const token = new URLSearchParams(window.location.hash.slice(1)).get('token')
    window.history.replaceState(null, '', window.location.pathname)
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