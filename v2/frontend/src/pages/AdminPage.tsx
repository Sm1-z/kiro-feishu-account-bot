import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Table, Tag, Space, Segmented, message, Popconfirm,
  Typography, Layout, Tabs,
} from 'antd'
import {
  adminRequests, approve, reject, getAccounts, ReqItem, AccountRow,
} from '../api'

const { Header, Content } = Layout
const { Title } = Typography

const TYPE_LABEL: any = { apply: '开通', upgrade: '升级', quota_increase: '配额' }
const STATUS_COLOR: any = {
  pending: 'orange', approved: 'blue', executed: 'green', failed: 'red', rejected: 'default',
}

// 用量空值（Athena 未配置/无数据）统一显示 —
const fmtNum = (v: number | null) =>
  v === null || v === undefined ? <span style={{ color: '#ccc' }}>—</span> : v.toLocaleString()
const fmtCredits = (v: number | null) =>
  v === null || v === undefined ? <span style={{ color: '#ccc' }}>—</span> : v.toFixed(2)

function RequestsPanel() {
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
  )
}

function AccountsPanel() {
  const [rows, setRows] = useState<AccountRow[]>([])
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try { setRows(await getAccounts()) }
    catch { message.error('加载失败') } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const roleTag = (r: string) =>
    r === 'primary' ? <Tag color="gold">主</Tag> : <Tag>副</Tag>

  const cols = [
    { title: '飞书用户', dataIndex: 'feishu_name', render: (v: string) => v || '—' },
    { title: '用户名', dataIndex: 'kiro_username' },
    { title: '分组', dataIndex: 'team' },
    { title: '主/副', dataIndex: 'account_role', render: roleTag,
      filters: [{ text: '主账号', value: 'primary' }, { text: '副账号', value: 'secondary' }],
      onFilter: (v: any, r: AccountRow) => r.account_role === v },
    { title: 'Tier', dataIndex: 'tier', render: (t: string) => t ? <Tag color="blue">{t}</Tag> : '—' },
    { title: '状态', dataIndex: 'status', render: (s: string) =>
      <Tag color={s === 'active' ? 'green' : 'default'}>{s}</Tag> },
    { title: 'Credits', dataIndex: 'usage_credits', align: 'right' as const,
      render: fmtCredits,
      sorter: (a: AccountRow, b: AccountRow) => (a.usage_credits ?? -1) - (b.usage_credits ?? -1) },
    { title: '消息数', dataIndex: 'usage_messages', align: 'right' as const,
      render: fmtNum,
      sorter: (a: AccountRow, b: AccountRow) => (a.usage_messages ?? -1) - (b.usage_messages ?? -1) },
    { title: '活跃天', dataIndex: 'usage_active_days', align: 'right' as const, render: fmtNum },
    { title: '最后活跃', dataIndex: 'usage_last_active', render: (v: string | null) =>
      v || <span style={{ color: '#ccc' }}>—</span> },
  ]

  return (
    <Card
      title="账号总览"
      extra={<Button onClick={load}>刷新</Button>}
    >
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -8 }}>
        用量数据来自 Kiro Analytics（Athena，约每日更新、缓存 5 分钟）；
        显示 — 表示该账号暂无用量记录或未配置数据源。可点列头按 Credits/消息数排序，
        筛选副账号识别低用量回收对象。
      </Typography.Paragraph>
      <Table rowKey="kiro_user_id" dataSource={rows} columns={cols} loading={loading}
        pagination={{ pageSize: 15 }} size="small" />
    </Card>
  )
}

export default function AdminPage() {
  const nav = useNavigate()
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Title level={4} style={{ margin: 0 }}>审批后台</Title>
        <Space>
          <Button onClick={() => nav('/')}>返回</Button>
        </Space>
      </Header>
      <Content style={{ padding: 24 }}>
        <Tabs
          defaultActiveKey="requests"
          items={[
            { key: 'requests', label: '申请审批', children: <RequestsPanel /> },
            { key: 'accounts', label: '账号总览', children: <AccountsPanel /> },
          ]}
        />
      </Content>
    </Layout>
  )
}
