import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Table, Tag, Space, Segmented, message, Popconfirm,
  Typography, Layout,
} from 'antd'
import { adminRequests, approve, reject, ReqItem } from '../api'

const { Header, Content } = Layout
const { Title } = Typography

const TYPE_LABEL: any = { apply: '开通', upgrade: '升级', quota_increase: '配额' }
const STATUS_COLOR: any = {
  pending: 'orange', approved: 'blue', executed: 'green', failed: 'red', rejected: 'default',
}

export default function AdminPage() {
  const nav = useNavigate()
  const [filter, setFilter] = useState<string>('pending')
  const [rows, setRows] = useState<ReqItem[]>([])
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      setRows(await adminRequests(filter === 'all' ? undefined : filter))
    } catch { message.error('加载失败') } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [filter])

  const doApprove = async (id: string) => {
    try { await approve(id); message.success('已受理，正在执行'); setTimeout(load, 1500) }
    catch (e: any) { message.error(e.response?.data?.detail || '失败') }
  }
  const doReject = async (id: string) => {
    try { await reject(id); message.success('已拒绝'); load() }
    catch (e: any) { message.error(e.response?.data?.detail || '失败') }
  }

  const cols = [
    { title: '申请人', dataIndex: 'user_name' },
    { title: '类型', dataIndex: 'type', render: (t: string) => TYPE_LABEL[t] || t },
    { title: '详情', dataIndex: 'payload', render: (p: any) =>
      <span style={{ fontSize: 12 }}>{JSON.stringify(p)}</span> },
    { title: '状态', dataIndex: 'status', render: (s: string) =>
      <Tag color={STATUS_COLOR[s]}>{s}</Tag> },
    { title: '结果', dataIndex: 'result', render: (r: any) =>
      r && Object.keys(r).length ? <span style={{ fontSize: 12 }}>{JSON.stringify(r)}</span> : '-' },
    { title: '操作', render: (_: any, r: ReqItem) =>
      r.status === 'pending' ? (
        <Space>
          <Popconfirm title="确认通过并执行开通？" onConfirm={() => doApprove(r.request_id)}>
            <Button type="primary" size="small">通过</Button>
          </Popconfirm>
          <Popconfirm title="确认拒绝？" onConfirm={() => doReject(r.request_id)}>
            <Button danger size="small">拒绝</Button>
          </Popconfirm>
        </Space>
      ) : <span style={{ color: '#aaa' }}>已处理</span> },
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Title level={4} style={{ margin: 0 }}>审批后台</Title>
        <Space>
          <Button onClick={() => nav('/')}>返回</Button>
        </Space>
      </Header>
      <Content style={{ padding: 24 }}>
        <Card
          title="申请审批"
          extra={
            <Space>
              <Segmented value={filter} onChange={(v) => setFilter(v as string)}
                options={[{ label: '待审批', value: 'pending' }, { label: '全部', value: 'all' }]} />
              <Button onClick={load}>刷新</Button>
            </Space>
          }
        >
          <Table rowKey="request_id" dataSource={rows} columns={cols} loading={loading}
            pagination={{ pageSize: 10 }} />
        </Card>
      </Content>
    </Layout>
  )
}
