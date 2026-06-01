import { Button, Card, Typography } from 'antd'

const { Title, Paragraph } = Typography

export default function LoginPage() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '100vh', background: '#f0f2f5' }}>
      <Card style={{ width: 420, textAlign: 'center' }}>
        <Title level={3}>Kiro 账号管理平台</Title>
        <Paragraph type="secondary">使用飞书账号登录，自助申请与管理 Kiro 账号</Paragraph>
        <Button type="primary" size="large" block href="/api/auth/feishu/login">
          飞书登录
        </Button>
      </Card>
    </div>
  )
}
