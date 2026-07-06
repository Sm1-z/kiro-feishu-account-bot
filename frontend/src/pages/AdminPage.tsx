// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Table, Tag, Space, Segmented, message, Popconfirm,
  Typography, Layout, Tabs, Row, Col, Statistic, Modal, InputNumber, Alert,
} from 'antd'
import {
  adminRequests, approve, reject, getAccounts, getOverageCap, raiseOverageCap,
  ReqItem, AccountRow, OverageCapInfo,
} from '../api'
import { HBarChart, DonutChart } from '../components/MiniCharts'

const { Header, Content } = Layout
const { Title } = Typography

const TYPE_LABEL: any = { apply: '开通', upgrade: '升级', quota_increase: '配额', overage_cap: '超额上限' }
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
  const [capInfo, setCapInfo] = useState<OverageCapInfo>({ cap: null, pending: null })
  const [capModalOpen, setCapModalOpen] = useState(false)
  const [capTarget, setCapTarget] = useState<number | null>(null)
  const [capSubmitting, setCapSubmitting] = useState(false)

  const loadCap = async () => {
    // cap 独立加载，失败不影响账号列表（后端已降级，这里再兜一层）
    try { setCapInfo(await getOverageCap()) } catch { setCapInfo({ cap: null, pending: null }) }
  }
  const load = async (force = false) => {
    setLoading(true)
    try { setRows(await getAccounts(force)) }
    catch { message.error('加载失败') } finally { setLoading(false) }
    loadCap()
  }
  useEffect(() => { load() }, [])

  const cap = capInfo.cap
  const capPending = capInfo.pending

  const submitCapRaise = async () => {
    if (capTarget == null) return
    setCapSubmitting(true)
    try {
      await raiseOverageCap(capTarget)
      message.success('已提交调高申请（小额通常即时生效）')
      setCapModalOpen(false)
      setCapTarget(null)
      loadCap()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '提交失败')
    } finally { setCapSubmitting(false) }
  }

  const people = aggregateByPerson(rows)

  // ── 指标卡 ──
  const totalAccounts = rows.length
  const totalPeople = people.length
  const totalCredits = rows.reduce((s, r) => s + (r.usage_credits ?? 0), 0)
  const activePeople = people.filter((p) => p.has_usage && p.credits > 0).length
  // Overages 上限是 profile 级单一值（USD/订阅）；最坏敞口 = 账号数 × 上限
  const worstOverage = cap != null ? totalAccounts * cap.value : null

  // ── 图表数据 ──
  const topByCredits = people.filter((p) => p.credits > 0)
    .slice(0, 8).map((p) => ({ label: p.feishu_name, value: +p.credits.toFixed(1) }))
  const tierDist = (() => {
    const m = new Map<string, number>()
    for (const r of rows) {
      const t = r.live_synced ? (r.live_tier || '无订阅') : (r.tier || '未知')
      m.set(t, (m.get(t) || 0) + 1)
    }
    return [...m.entries()].map(([label, value]) => ({ label, value }))
  })()

  // ── 子账号明细（展开）──
  // Tier/状态优先展示订阅实况（控制台直接退订/改套餐不回写映射表，快照会滞后）
  const subCols = [
    { title: '用户名', dataIndex: 'kiro_username' },
    { title: '主/副', dataIndex: 'account_role', render: roleTag },
    { title: 'Tier', dataIndex: 'live_tier', render: (t: string | null, r: AccountRow) => {
      if (!r.live_synced) return r.tier ? <Tag color="blue">{r.tier}</Tag> : '—'
      return t ? <Tag color="blue">{t}</Tag> : <span style={{ color: '#ccc' }}>—</span>
    } },
    { title: '分组', dataIndex: 'team' },
    { title: '订阅状态', dataIndex: 'live_status', render: (s: string | null, r: AccountRow) => {
      if (!r.live_synced)
        return <Tag color={r.status === 'active' ? 'green' : 'default'}>{r.status}（快照）</Tag>
      if (!s) return <Tag color="red">无订阅</Tag>
      return <Tag color={s === 'ACTIVE' ? 'green' : s === 'PENDING' ? 'orange' : 'default'}>{s}</Tag>
    } },
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
        <Col span={4}><Card size="small"><Statistic title="飞书用户数" value={totalPeople} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="账号总数" value={totalAccounts} /></Card></Col>
        <Col span={5}><Card size="small"><Statistic title="总 Credits" value={totalCredits} precision={1} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="有用量的人" value={activePeople} suffix={`/ ${totalPeople}`} /></Card></Col>
        <Col span={7}>
          <Card size="small">
            <Statistic
              title={
                <Space size={4}>
                  超额上限 / 订阅
                  {cap && !capPending && (
                    <Typography.Link onClick={() => setCapModalOpen(true)} style={{ fontSize: 12 }}>
                      调高
                    </Typography.Link>
                  )}
                  {capPending && (
                    <Tag color="orange" style={{ fontSize: 11, marginLeft: 4 }}>
                      审批中 → ${capPending.desired_value}
                    </Tag>
                  )}
                </Space>
              }
              value={cap != null ? cap.value : '—'}
              prefix={cap != null ? '$' : undefined}
              valueStyle={cap == null ? { color: '#ccc' } : undefined}
            />
            {worstOverage != null && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                最坏月超额敞口 ${worstOverage.toLocaleString()}（{totalAccounts} 账号 × ${cap!.value}，需在 Kiro 控制台开启 Overages 才生效）。
                上限只可调高，调低需联系 AWS Support。
              </Typography.Text>
            )}
          </Card>
        </Col>
      </Row>

      {/* 调高超额上限 */}
      <Modal
        title="调高超额上限"
        open={capModalOpen}
        onCancel={() => { setCapModalOpen(false); setCapTarget(null) }}
        footer={[
          <Button key="cancel" onClick={() => { setCapModalOpen(false); setCapTarget(null) }}>取消</Button>,
          <Popconfirm key="ok" title={`确认调高至 $${capTarget ?? '?'}？此操作不可自助回退`}
            onConfirm={submitCapRaise} disabled={capTarget == null}>
            <Button type="primary" danger loading={capSubmitting} disabled={capTarget == null}>
              确认调高
            </Button>
          </Popconfirm>,
        ]}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert type="warning" showIcon message="上限只可调高，不可调低"
            description="提交后无法在平台或 AWS 控制台自助降回，调低需开 AWS Support case。请确认新值。" />
          <div>
            当前值：<b>${cap?.value}</b> / 订阅（profile 级，对所有用户生效）
            {cap != null && (
              <div style={{ color: '#999', fontSize: 12 }}>
                单次最多调高至当前值 2 倍（${cap.value * 2}）；新的最坏月超额敞口 = 账号数 × 新上限
              </div>
            )}
          </div>
          <InputNumber
            style={{ width: '100%' }}
            prefix="$"
            placeholder={cap != null ? `大于 ${cap.value}，不超过 ${cap.value * 2}` : ''}
            min={cap != null ? cap.value + 1 : 1}
            max={cap != null ? cap.value * 2 : undefined}
            value={capTarget}
            onChange={(v) => setCapTarget(v)}
          />
          {capTarget != null && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              调高后最坏月超额敞口：${(totalAccounts * capTarget).toLocaleString()}（{totalAccounts} 账号 × ${capTarget}）
            </Typography.Text>
          )}
        </Space>
      </Modal>

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
        extra={<Button size="small" loading={loading} onClick={() => load(true)}>刷新</Button>}>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
          一人多账号已合并为一行，Credits/消息为名下所有账号汇总；点行首 ▸ 展开看每个子账号。
          Tier/订阅状态为 AWS 实时数据（控制台的退订/改套餐也会反映）；
          用量来自 Kiro Analytics（Athena，约每日更新），点「刷新」强制重查。— 表示暂无数据。
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