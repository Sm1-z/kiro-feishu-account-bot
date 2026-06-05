import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Table, Tag, Space, Segmented, message, Popconfirm,
  Typography, Layout, Tabs, Row, Col, Statistic,
} from 'antd'
import {
  adminRequests, approve, reject, getAccounts, ReqItem, AccountRow,
} from '../api'
import { HBarChart, DonutChart } from '../components/MiniCharts'

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

// 一个飞书人聚合后的视图（名下多账号汇总）
interface PersonRow {
  feishu_open_id: string
  feishu_name: string
  account_count: number
  credits: number          // 名下所有账号 credits 之和
  messages: number
  has_usage: boolean       // 是否有任一账号拿到用量
  last_active: string
  accounts: AccountRow[]   // 子账号明细（展开用）
}

function aggregateByPerson(rows: AccountRow[]): PersonRow[] {
  const map = new Map<string, PersonRow>()
  for (const r of rows) {
    const key = r.feishu_open_id || r.kiro_user_id
    let p = map.get(key)
    if (!p) {
      p = { feishu_open_id: r.feishu_open_id, feishu_name: r.feishu_name || '—',
            account_count: 0, credits: 0, messages: 0, has_usage: false,
            last_active: '', accounts: [] }
      map.set(key, p)
    }
    p.accounts.push(r)
    p.account_count += 1
    if (r.usage_credits != null) { p.credits += r.usage_credits; p.has_usage = true }
    if (r.usage_messages != null) { p.messages += r.usage_messages; p.has_usage = true }
    if (r.usage_last_active && r.usage_last_active > p.last_active) p.last_active = r.usage_last_active
  }
  return [...map.values()].sort((a, b) => b.credits - a.credits)
}

const roleTag = (r: string) =>
  r === 'primary' ? <Tag color="gold">主</Tag> : <Tag>副</Tag>

function AccountsPanel() {
  const [rows, setRows] = useState<AccountRow[]>([])
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try { setRows(await getAccounts()) }
    catch { message.error('加载失败') } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const people = aggregateByPerson(rows)

  // ── 指标卡 ──
  const totalAccounts = rows.length
  const totalPeople = people.length
  const totalCredits = rows.reduce((s, r) => s + (r.usage_credits ?? 0), 0)
  const activePeople = people.filter((p) => p.has_usage && p.credits > 0).length

  // ── 图表数据 ──
  const topByCredits = people.filter((p) => p.credits > 0)
    .slice(0, 8).map((p) => ({ label: p.feishu_name, value: +p.credits.toFixed(1) }))
  const tierDist = (() => {
    const m = new Map<string, number>()
    for (const r of rows) { const t = r.tier || '未知'; m.set(t, (m.get(t) || 0) + 1) }
    return [...m.entries()].map(([label, value]) => ({ label, value }))
  })()

  // ── 子账号明细（展开）──
  const subCols = [
    { title: '用户名', dataIndex: 'kiro_username' },
    { title: '主/副', dataIndex: 'account_role', render: roleTag },
    { title: 'Tier', dataIndex: 'tier', render: (t: string) => t ? <Tag color="blue">{t}</Tag> : '—' },
    { title: '分组', dataIndex: 'team' },
    { title: '状态', dataIndex: 'status', render: (s: string) =>
      <Tag color={s === 'active' ? 'green' : 'default'}>{s}</Tag> },
    { title: 'Credits', dataIndex: 'usage_credits', align: 'right' as const, render: fmtCredits },
    { title: '消息数', dataIndex: 'usage_messages', align: 'right' as const, render: fmtNum },
    { title: '最后活跃', dataIndex: 'usage_last_active',
      render: (v: string | null) => v || <span style={{ color: '#ccc' }}>—</span> },
  ]

  // ── 按人聚合主表 ──
  const personCols = [
    { title: '飞书用户', dataIndex: 'feishu_name' },
    { title: '账号数', dataIndex: 'account_count', align: 'right' as const,
      render: (n: number) => <Tag>{n}</Tag> },
    { title: 'Credits（汇总）', dataIndex: 'credits', align: 'right' as const,
      defaultSortOrder: 'descend' as const,
      render: (v: number, p: PersonRow) => p.has_usage ? v.toFixed(2) : <span style={{ color: '#ccc' }}>—</span>,
      sorter: (a: PersonRow, b: PersonRow) => a.credits - b.credits },
    { title: '消息数（汇总）', dataIndex: 'messages', align: 'right' as const,
      render: (v: number, p: PersonRow) => p.has_usage ? v.toLocaleString() : <span style={{ color: '#ccc' }}>—</span>,
      sorter: (a: PersonRow, b: PersonRow) => a.messages - b.messages },
    { title: '最后活跃', dataIndex: 'last_active',
      render: (v: string) => v || <span style={{ color: '#ccc' }}>—</span> },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {/* 指标卡 */}
      <Row gutter={16}>
        <Col span={6}><Card size="small"><Statistic title="飞书用户数" value={totalPeople} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="账号总数" value={totalAccounts} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="总 Credits" value={totalCredits} precision={1} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="有用量的人" value={activePeople} suffix={`/ ${totalPeople}`} /></Card></Col>
      </Row>

      {/* 图表 */}
      <Row gutter={16}>
        <Col span={14}>
          <Card size="small" title="Top 用户 · Credits（按人汇总）">
            <HBarChart data={topByCredits} />
          </Card>
        </Col>
        <Col span={10}>
          <Card size="small" title="账号 Tier 分布">
            <DonutChart data={tierDist} />
          </Card>
        </Col>
      </Row>

      {/* 按人聚合表（可展开下钻子账号） */}
      <Card size="small" title="按飞书用户聚合（展开看名下各账号）"
        extra={<Button size="small" onClick={load}>刷新</Button>}>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
          一人多账号已合并为一行，Credits/消息为名下所有账号汇总；点行首 ▸ 展开看每个子账号。
          用量来自 Kiro Analytics（Athena，约每日更新、缓存 5 分钟），— 表示暂无用量数据。
        </Typography.Paragraph>
        <Table
          rowKey="feishu_open_id"
          dataSource={people}
          columns={personCols}
          loading={loading}
          size="small"
          pagination={{ pageSize: 15 }}
          expandable={{
            expandedRowRender: (p: PersonRow) => (
              <Table rowKey="kiro_user_id" dataSource={p.accounts} columns={subCols}
                size="small" pagination={false} />
            ),
            rowExpandable: (p: PersonRow) => p.accounts.length > 0,
          }}
        />
      </Card>
    </Space>
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
