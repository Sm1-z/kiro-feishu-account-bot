// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Table, Tag, Space, Segmented, message, Popconfirm,
  Typography, Layout, Tabs, Row, Col, Statistic, Modal, InputNumber, Alert, Input, Select,
  Checkbox, Progress, Descriptions,
} from 'antd'
import {
  adminRequests, approve, reject, getAccounts, getGroups, createGroup, getUnlinked, getFeishuUsers,
  linkUnlinked, deactivateAccounts, reactivateAccounts, retrySeats, getUsageHistory, getPool, setLimit,
  isAccountActive, LIMIT_SCOPE_LABEL,
  ReqItem, AccountRow, UnlinkedAccount, FeishuUser, BatchResult, UsageHistory, PoolInfo,
} from '../api'
import { HBarChart, DonutChart } from '../components/MiniCharts'

const { Header, Content } = Layout
const { Title } = Typography

const TYPE_LABEL: Record<string, string> = {
  apply: '开通', limit_change: '调整上限', limit_set: '设置上限',
  deactivate: '停用（移出池）', recycle: '回收', reactivate: '恢复', seat_retry: '重新分配席位',
}
const STATUS_COLOR: Record<string, string> = {
  pending: 'orange', approved: 'blue', executed: 'green', failed: 'red', rejected: 'default',
}

const dash = <span style={{ color: '#ccc' }}>—</span>
const fmtNum = (v: number | null | undefined) => (v === null || v === undefined ? dash : v.toLocaleString())
const fmtCredits = (v: number | null | undefined) =>
  v === null || v === undefined ? dash : v.toLocaleString(undefined, { maximumFractionDigits: 1 })
const fmtDate = (iso: string | null | undefined) => (iso ? iso.slice(0, 10) : '—')

// 积分池实况优先；实况不可得时按快照
const effPool = (r: AccountRow) =>
  r.live_synced ? (r.pool_status || 'NONE') : (r.status === 'active' ? 'SNAPSHOT_ACTIVE' : 'NONE')
const poolTag = (r: AccountRow) => {
  const s = effPool(r)
  if (s === 'SEATED') return <Tag color="green">已占座</Tag>
  if (s === 'AWAITING_SEAT') return <Tag color="orange">等待席位</Tag>
  if (s === 'SNAPSHOT_ACTIVE') return <Tag color="green">开通（快照）</Tag>
  return <Tag color="red">不在池中</Tag>
}
const limitCell = (r: AccountRow) => {
  if (r.limit_scope == null) return dash
  if (r.limit_scope === 'NONE') return <Tag>无上限</Tag>
  return <span>{r.limit?.toLocaleString()} <Tag style={{ marginLeft: 2, fontSize: 11 }}
    color={r.limit_scope === 'USER' ? 'blue' : undefined}>{LIMIT_SCOPE_LABEL[r.limit_scope]}</Tag></span>
}
const usedCell = (r: AccountRow) => {
  if (r.month_credits == null) return dash
  const pct = r.limit ? r.month_credits / r.limit : 0
  const color = r.limit === 0 || pct >= 1 ? '#cf1322' : pct >= 0.8 ? '#d46b08' : undefined
  return <span style={{ color }}>{fmtCredits(r.month_credits)}{r.limit ? ` / ${r.limit.toLocaleString()}` : ''}</span>
}

const reqDetail = (r: ReqItem) => {
  const p = r.payload || {}
  switch (r.type) {
    case 'apply': return `${p.username} · ${p.email} · ${p.group || '默认组'} · 上限 ${p.usage_limit ? `${p.usage_limit} credits/月` : '默认'}${p.reason ? ` · 理由：${p.reason}` : ''}`
    case 'limit_change': return `${p.username}：${p.current_limit ?? '默认'} → ${p.requested_limit} credits/月${p.reason ? ` · 理由：${p.reason}` : ''}`
    case 'limit_set': return `${p.scope === 'POOL' ? 'profile 默认上限' : p.username}：${p.from} → ${p.to}`
    default: return p.username || JSON.stringify(p)
  }
}

const downloadCsv = (filename: string, header: string[], rows: (string | number | null | undefined)[][]) => {
  const esc = (v: any) => `"${String(v ?? '').replace(/"/g, '""')}"`
  const csv = '﻿' + [header, ...rows].map((r) => r.map(esc).join(',')).join('\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}

/** 历史月用量弹窗：按 UTC 自然月回溯某账号的 credits / 消息 / 活跃天数。 */
function UsageHistoryModal({ row, onClose }: { row: AccountRow | null; onClose: () => void }) {
  const now = new Date()
  const [year, setYear] = useState(now.getUTCFullYear())
  const [month, setMonth] = useState(now.getUTCMonth() + 1)
  const [data, setData] = useState<UsageHistory | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const query = async (y: number, m: number) => {
    if (!row) return
    setLoading(true); setErr(''); setData(null)
    try { setData(await getUsageHistory(row.kiro_user_id, y, m)) }
    catch (e: any) { setErr(e.response?.data?.detail || '查询失败') }
    finally { setLoading(false) }
  }
  useEffect(() => {
    if (row) { setYear(now.getUTCFullYear()); setMonth(now.getUTCMonth() + 1); query(now.getUTCFullYear(), now.getUTCMonth() + 1) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row])

  const years = [now.getUTCFullYear() - 1, now.getUTCFullYear()]
  return (
    <Modal title={`用量回溯 · ${row?.kiro_username || ''}`} open={!!row} onCancel={onClose} footer={null} width={520}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Space>
          <Select value={year} onChange={(v) => { setYear(v); query(v, month) }} style={{ width: 100 }}
            options={years.map((y) => ({ value: y, label: `${y}` }))} />
          <Select value={month} onChange={(v) => { setMonth(v); query(year, v) }} style={{ width: 90 }}
            options={Array.from({ length: 12 }, (_, i) => ({ value: i + 1, label: `${i + 1} 月` }))} />
          <Button size="small" loading={loading} onClick={() => query(year, month)}>查询</Button>
        </Space>
        {err && <Alert type="error" showIcon message={err} />}
        {data && !data.available && (
          <Alert type="warning" showIcon message="用量报表未配置或暂不可用"
            description="用量回溯依赖 Kiro 的 User Activity Report（S3 → Glue → Athena）。请确认已开启报表并配置 Athena。" />
        )}
        {data && data.available && !data.has_usage_entry && (
          <Alert type="info" showIcon message={`${year} 年 ${month} 月无该账号的用量记录`}
            description="该月没有任何活动，或报表尚未沉淀该月数据（日级延迟约 1 天）。" />
        )}
        {data && data.has_usage_entry && (
          <Row gutter={12}>
            <Col span={8}><Card size="small"><Statistic title="已用 credits" value={data.credit_used ?? '—'} precision={1} /></Card></Col>
            <Col span={8}><Card size="small"><Statistic title="消息数" value={data.messages ?? '—'} /></Card></Col>
            <Col span={8}><Card size="small"><Statistic title="活跃天数" value={data.active_days ?? '—'} suffix="天" /></Card></Col>
          </Row>
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          数据来自 User Activity Report（按 UTC 自然月，日级延迟约 1 天）。当前生效上限：{row ? limitCell(row) : '—'}
        </Typography.Text>
      </Space>
    </Modal>
  )
}

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
  const approveTitle = (r: ReqItem) =>
    r.type === 'apply' ? '确认通过？将建账号并加入积分池（后付池每人 $20/月，首月按天折算）'
      : r.type === 'limit_change' ? `确认把 ${r.payload?.username} 的上限调到 ${r.payload?.requested_limit} credits/月？即时生效`
        : '确认通过？'

  const cols = [
    { title: '申请人', dataIndex: 'user_name', width: 90 },
    { title: '类型', dataIndex: 'type', width: 110, render: (t: string) => TYPE_LABEL[t] || t },
    { title: '详情', render: (_: any, r: ReqItem) => <span style={{ fontSize: 12 }}>{reqDetail(r)}</span> },
    { title: '状态', dataIndex: 'status', width: 90, render: (s: string) => <Tag color={STATUS_COLOR[s]}>{s}</Tag> },
    { title: '结果', render: (_: any, r: ReqItem) => {
      const res = r.result || {}
      if (res.error) return <Typography.Text type="danger" style={{ fontSize: 12 }}>{res.error}</Typography.Text>
      if (res.warning) return <Typography.Text type="warning" style={{ fontSize: 12 }}>{res.warning}</Typography.Text>
      if (r.review_comment) return <span style={{ fontSize: 12 }}>{r.review_comment}</span>
      return Object.keys(res).length ? <span style={{ fontSize: 12, color: '#888' }}>{JSON.stringify(res)}</span> : '-'
    } },
    { title: '操作', width: 140, render: (_: any, r: ReqItem) =>
      r.status === 'pending' ? (
        <Space>
          <Popconfirm title={approveTitle(r)} onConfirm={() => doApprove(r.request_id)}>
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
            options={[{ label: '待审批', value: 'pending' }, { label: '全部（含操作审计）', value: 'all' }]} />
          <Button onClick={load}>刷新</Button>
        </Space>
      }
    >
      <Table rowKey="request_id" dataSource={rows} columns={cols} loading={loading}
        pagination={{ pageSize: 10 }} />
    </Card>
  )
}

const BATCH_LIMIT = 200           // 后端单次上限；超出前端自动分批
const CONFIRM_TYPE_THRESHOLD = 10 // 名单 ≥ 此数时，确认须手动输入账号数

/** 停用 / 回收弹窗 = 「核对名单 + 确认」。停用即移出积分池，立即生效。每人一个账号，停用即此人停用 Kiro。 */
function DeactivateModal({ open, targets, summary, onClose, onDone }: {
  open: boolean
  targets: AccountRow[]
  summary?: string
  onClose: () => void
  onDone: () => void
}) {
  const [rows, setRows] = useState<AccountRow[]>([])
  const [recycle, setRecycle] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [results, setResults] = useState<BatchResult[] | null>(null)
  const [kw, setKw] = useState('')
  const [typed, setTyped] = useState('')

  useEffect(() => {
    if (open) { setRows(targets); setRecycle(false); setResults(null); setKw(''); setTyped('') }
  }, [open, targets])

  const n = rows.length
  const byTeam = (() => {
    const m = new Map<string, number>()
    for (const r of rows) { const t = r.team || '（无分组）'; m.set(t, (m.get(t) || 0) + 1) }
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  })()
  const monthTotal = rows.reduce((s, r) => s + (r.month_credits ?? 0), 0)
  const shown = rows.filter((r) =>
    !kw || `${r.kiro_username} ${r.feishu_name} ${r.team}`.toLowerCase().includes(kw.toLowerCase()))
  const remove = (id: string) => setRows((cur) => cur.filter((r) => r.kiro_user_id !== id))
  const needType = n >= CONFIRM_TYPE_THRESHOLD
  const typedOk = !needType || typed.trim() === String(n)

  const submit = async () => {
    if (!n) return
    setSubmitting(true)
    try {
      const ids = rows.map((r) => r.kiro_user_id)
      const all: BatchResult[] = []
      for (let i = 0; i < ids.length; i += BATCH_LIMIT) {
        const resp = await deactivateAccounts(ids.slice(i, i + BATCH_LIMIT), recycle)
        all.push(...resp.results)
      }
      const succeeded = all.filter((r) => r.success).length
      const failed = all.length - succeeded
      setResults(all)
      if (failed === 0) message.success(`${recycle ? '回收' : '停用'}完成：${succeeded} 个账号`)
      else message.warning(`完成 ${succeeded} 个，失败 ${failed} 个，详见下方`)
      onDone()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '提交失败')
    } finally { setSubmitting(false) }
  }

  const scope = byTeam.length === 1 ? `分组 ${byTeam[0][0]} 的 ` : byTeam.length > 1 ? `${byTeam.length} 个分组共 ` : ''
  const confirmTitle = recycle
    ? `确认回收${scope}${n} 个账号？移出积分池 + 删除 IDC 用户 + 解除映射，不可恢复`
    : `确认停用${scope}${n} 个账号？立即移出积分池，这些人马上失去 Kiro 访问`

  const exportResults = () => results && downloadCsv(
    `kiro-${recycle ? 'recycle' : 'deactivate'}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`,
    ['username', 'user_id', 'success', 'error', 'recycle'],
    results.map((r) => [r.username, r.user_id, r.success ? 'success' : 'failed', r.error, recycle ? 'yes' : 'no']))
  const exportList = () => downloadCsv(
    `kiro-deactivate-list-${new Date().toISOString().slice(0, 10)}.csv`,
    ['username', 'feishu_name', 'team', 'pool_status', 'limit', 'month_credits'],
    rows.map((r) => [r.kiro_username, r.feishu_name, r.team, effPool(r), r.limit, r.month_credits]))

  const reviewCols = [
    { title: '飞书用户', dataIndex: 'feishu_name', render: (v: string) => v || '—' },
    { title: '用户名', dataIndex: 'kiro_username', sorter: (a: AccountRow, b: AccountRow) => a.kiro_username.localeCompare(b.kiro_username) },
    { title: '分组', dataIndex: 'team', render: (v: string) => v || '—',
      sorter: (a: AccountRow, b: AccountRow) => (a.team || '').localeCompare(b.team || '') },
    { title: '积分池', key: 'pool', render: (_: any, r: AccountRow) => poolTag(r) },
    { title: '本月已用', dataIndex: 'month_credits', align: 'right' as const, render: fmtCredits,
      sorter: (a: AccountRow, b: AccountRow) => (a.month_credits ?? 0) - (b.month_credits ?? 0) },
    { title: '', key: 'rm', width: 56, render: (_: any, r: AccountRow) => (
      <Button size="small" type="text" danger onClick={() => remove(r.kiro_user_id)} aria-label={`移除 ${r.kiro_username}`}>移除</Button>
    ) },
  ]

  return (
    <Modal
      title={recycle ? '回收账号' : '停用账号（移出积分池）'}
      open={open}
      onCancel={onClose}
      width={860}
      footer={results ? [
        <Button key="csv" onClick={exportResults}>导出结果 CSV</Button>,
        <Button key="close" type="primary" onClick={onClose}>关闭</Button>,
      ] : [
        <Button key="cancel" onClick={onClose}>取消</Button>,
        <Popconfirm key="ok" title={confirmTitle} onConfirm={submit} disabled={!n || !typedOk}>
          <Button type="primary" danger loading={submitting} disabled={!n || !typedOk}>
            {recycle ? '确认回收' : '确认停用'}{n ? `（${n}）` : ''}
          </Button>
        </Popconfirm>,
      ]}
    >
      {results ? (
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Alert type={results.every((r) => r.success) ? 'success' : 'warning'} showIcon
            message={`${recycle ? '回收' : '停用'}完成：成功 ${results.filter((r) => r.success).length} / 失败 ${results.filter((r) => !r.success).length}`} />
          <Table rowKey="user_id" dataSource={results} size="small" pagination={{ pageSize: 10 }}
            columns={[
              { title: '用户名', dataIndex: 'username', render: (v: string, r: BatchResult) => v || r.user_id },
              { title: '结果', dataIndex: 'success', width: 80,
                render: (ok: boolean) => ok ? <Tag color="green">成功</Tag> : <Tag color="red">失败</Tag>,
                sorter: (a: BatchResult, b: BatchResult) => Number(a.success) - Number(b.success) },
              { title: '说明', dataIndex: 'error', render: (e: string) => e || '—' },
            ]} />
        </Space>
      ) : (
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          {summary && <Alert type="info" showIcon message={summary} />}
          <Card size="small" data-testid="deact-summary">
            <Space wrap size={[16, 6]}>
              <span>共 <b style={{ fontSize: 16 }}>{n}</b> 个账号（{n} 人）</span>
              <span>分组：{byTeam.length ? byTeam.map(([t, c]) => `${t} ${c}`).join(' · ') : '—'}</span>
              <span>本月已用合计 <b>{fmtCredits(monthTotal)}</b> credits</span>
              <Typography.Link onClick={exportList} style={{ fontSize: 12 }}>导出名单</Typography.Link>
            </Space>
          </Card>
          <div>
            <Space style={{ marginBottom: 8 }} wrap>
              <Input.Search allowClear placeholder="按用户名 / 飞书用户 / 分组过滤" style={{ width: 280 }}
                value={kw} onChange={(e) => setKw(e.target.value)} />
              <span style={{ fontSize: 12, color: '#888' }}>显示 {shown.length} / {n}；要补选请关闭弹窗回列表勾选</span>
            </Space>
            <Table rowKey="kiro_user_id" dataSource={shown} columns={reviewCols} size="small"
              pagination={{ pageSize: 10, size: 'small', showTotal: (t) => `共 ${t} 行` }}
              locale={{ emptyText: n ? '无匹配' : '名单为空' }} />
          </div>
          <Alert type="warning" showIcon message="停用 = 立即移出积分池"
            description="每人只有一个账号，停用后这个人马上无法使用 Kiro；已用的 credits 不退，本账期席位费照收，下个账期起不再计费。预付池空出的席位留给其他人用，要少付席位费需在 Kiro 控制台删除空闲席位。若用户还通过某个已加入积分池的组获得访问，需同时把他移出该组。" />
          <Checkbox checked={recycle} onChange={(e) => setRecycle(e.target.checked)}>
            同时删除 IDC 用户并解除飞书映射（回收账号，<b>不可恢复</b>；此人之后可以重新申请）
          </Checkbox>
          {recycle && (
            <Alert type="error" showIcon message="回收会删除 IAM Identity Center 用户"
              description="用户的登录身份随即消失，无法再登录 Kiro；个人上限一并清除。通常用于离职或长期不用的人员。" />
          )}
          {needType && (
            <Alert type="warning" showIcon
              message={<Space>
                <span>名单较多（{n} 个）。请输入账号数 <b>{n}</b> 以启用确认按钮：</span>
                <Input size="small" style={{ width: 90 }} value={typed} onChange={(e) => setTyped(e.target.value)}
                  placeholder={String(n)} data-testid="confirm-count" status={typed && !typedOk ? 'error' : undefined} />
              </Space>} />
          )}
        </Space>
      )}
    </Modal>
  )
}

/** 设置上限弹窗：个人上限（scope=USER）或 profile 默认上限（scope=POOL）。 */
function LimitModal({ target, onClose, onDone }: {
  target: { scope: 'USER' | 'POOL'; row?: AccountRow; current: number | null } | null
  onClose: () => void
  onDone: () => void
}) {
  const [value, setValue] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)
  useEffect(() => { setValue(target?.current ?? null) }, [target])
  if (!target) return null
  const isUser = target.scope === 'USER'
  const save = async (v: number | null) => {
    setSaving(true)
    try {
      await setLimit(target.scope, target.row?.kiro_user_id || '', v)
      message.success(v == null ? '已删除上限' : `上限已设为 ${v.toLocaleString()} credits/月`)
      onDone(); onClose()
    } catch (e: any) { message.error(e.response?.data?.detail || '设置失败') } finally { setSaving(false) }
  }
  return (
    <Modal open title={isUser ? `个人上限 · ${target.row?.kiro_username}` : 'profile 默认上限（每人）'}
      onCancel={onClose}
      footer={[
        <Popconfirm key="rm" title={isUser ? '删除个人上限？该账号回落到组上限或 profile 默认上限' : '删除 profile 默认上限？没有个人 / 组上限的账号将不受限制'}
          onConfirm={() => save(null)} disabled={target.current == null}>
          <Button danger disabled={target.current == null} loading={saving}>删除上限</Button>
        </Popconfirm>,
        <Button key="cancel" onClick={onClose}>取消</Button>,
        <Button key="ok" type="primary" loading={saving} disabled={value == null} onClick={() => save(value)}>保存</Button>,
      ]}>
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
          生效优先级：个人上限 &gt; 所在组里最高的组上限 &gt; profile 默认上限 &gt; 无上限。
          {isUser ? ' 个人上限可以比组上限高或低。' : ' profile 默认上限按人计（每人每月），不是整个池子的总额。'}
          上限不能超过积分池当前的 credits 总量；填 0 表示禁止使用。即时生效，到上限后当月暂停。
        </Typography.Paragraph>
        {isUser && target.row && (
          <span style={{ fontSize: 12 }}>当前生效：{limitCell(target.row)}　本月已用：{usedCell(target.row)}</span>
        )}
        <InputNumber min={0} max={1000000} precision={0} style={{ width: '100%' }} value={value}
          onChange={(v) => setValue(v)} addonAfter="credits / 月" placeholder="例如 1000" />
      </Space>
    </Modal>
  )
}

function AccountsPanel({ prepaid }: { prepaid: boolean }) {
  const [rows, setRows] = useState<AccountRow[]>([])
  const [loading, setLoading] = useState(false)
  const [deactOpen, setDeactOpen] = useState(false)
  const [deactTargets, setDeactTargets] = useState<AccountRow[]>([])
  const [deactSummary, setDeactSummary] = useState<string | undefined>(undefined)
  const [historyRow, setHistoryRow] = useState<AccountRow | null>(null)
  const [limitTarget, setLimitTarget] = useState<{ scope: 'USER'; row: AccountRow; current: number | null } | null>(null)
  const [fTeams, setFTeams] = useState<string[]>([])
  const [fPool, setFPool] = useState<string | undefined>(undefined)
  const [fScope, setFScope] = useState<string | undefined>(undefined)
  const [fCreditsLt, setFCreditsLt] = useState<number | null>(null)
  const [fInactiveDays, setFInactiveDays] = useState<number | null>(null)
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])

  const load = async (force = false) => {
    setLoading(true)
    try { setRows(await getAccounts(force)) }
    catch { message.error('加载失败') } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const openDeactivate = (targets: AccountRow[], summary?: string) => {
    setDeactTargets(targets); setDeactSummary(summary); setDeactOpen(true)
  }
  const doReactivate = async (r: AccountRow) => {
    try {
      const one = (await reactivateAccounts([r.kiro_user_id])).results[0]
      if (!one?.success) message.error(one?.error || '恢复失败')
      else if (one.warning) message.warning(`${r.kiro_username} 已重新加入积分池，但${one.warning}`)
      else message.success(`已恢复 ${r.kiro_username}（重新加入积分池）`)
      load(true)
    } catch (e: any) { message.error(e.response?.data?.detail || '恢复失败') }
  }
  const doRetrySeat = async (r: AccountRow) => {
    try {
      const one = (await retrySeats([r.kiro_user_id])).results[0]
      if (one?.success) message.success(`已为 ${r.kiro_username} 分配席位`)
      else message.error(one?.error || '分配失败')
      load(true)
    } catch (e: any) { message.error(e.response?.data?.detail || '分配失败') }
  }

  const inPool = rows.filter(isAccountActive)
  const awaiting = rows.filter((r) => r.live_synced && r.pool_status === 'AWAITING_SEAT')
  const nearLimit = rows.filter((r) => r.limit_used_pct != null && r.limit_used_pct >= 80)
  const totalCredits = rows.reduce((s, r) => s + (r.month_credits ?? 0), 0)
  const activeUsers = rows.filter((r) => (r.month_credits ?? 0) > 0).length
  const nameOf = (r: AccountRow) => r.feishu_name || r.kiro_username

  const topByCredits = [...rows].filter((r) => (r.month_credits ?? 0) > 0)
    .sort((a, b) => (b.month_credits ?? 0) - (a.month_credits ?? 0))
    .slice(0, 8).map((r) => ({ label: nameOf(r), value: +(r.month_credits ?? 0).toFixed(1) }))
  const scopeDist = (() => {
    const m = new Map<string, number>()
    for (const r of inPool) {
      const k = r.limit_scope ? LIMIT_SCOPE_LABEL[r.limit_scope] : '未知'
      m.set(k, (m.get(k) || 0) + 1)
    }
    return [...m.entries()].map(([label, value]) => ({ label, value }))
  })()

  const daysSince = (d: string | null) => d ? Math.floor((Date.now() - new Date(d).getTime()) / 86400000) : Infinity
  const allTeams = [...new Set(rows.map((r) => r.team).filter(Boolean))].sort()
  const filtered = rows.filter((r) =>
    (!fTeams.length || fTeams.includes(r.team)) &&
    (!fPool || effPool(r) === fPool) &&
    (!fScope || (r.limit_scope || 'UNKNOWN') === fScope) &&
    (fCreditsLt == null || (r.month_credits ?? 0) < fCreditsLt) &&
    (fInactiveDays == null || daysSince(r.usage_last_active) >= fInactiveDays))
  const filterDesc = [
    fTeams.length ? `分组 ${fTeams.join('/')}` : '',
    fPool ? `积分池 ${fPool}` : '',
    fScope ? `上限来源 ${LIMIT_SCOPE_LABEL[fScope] || fScope}` : '',
    fCreditsLt != null ? `本月 Credits < ${fCreditsLt}` : '',
    fInactiveDays != null ? `${fInactiveDays} 天未活跃` : '',
  ].filter(Boolean).join(' · ')
  const resetFilters = () => { setFTeams([]); setFPool(undefined); setFScope(undefined); setFCreditsLt(null); setFInactiveDays(null) }
  const selectOf = (list: AccountRow[]) => setSelectedKeys(list.filter(isAccountActive).map((r) => r.kiro_user_id))
  const presetLowUsage = () => {
    const th = fCreditsLt ?? 50
    const days = fInactiveDays ?? 14
    setFCreditsLt(th); setFInactiveDays(days)
    selectOf(rows.filter((r) => (!fTeams.length || fTeams.includes(r.team)) &&
      (r.month_credits ?? 0) < th && daysSince(r.usage_last_active) >= days))
  }
  const selectedRows = rows.filter((r) => selectedKeys.includes(r.kiro_user_id))

  const cols = [
    { title: '飞书用户', dataIndex: 'feishu_name', render: (v: string) => v || '—',
      sorter: (a: AccountRow, b: AccountRow) => (a.feishu_name || '').localeCompare(b.feishu_name || '') },
    { title: '用户名', dataIndex: 'kiro_username' },
    { title: '分组', dataIndex: 'team' },
    { title: '积分池', key: 'pool', render: (_: any, r: AccountRow) => poolTag(r) },
    { title: '本月上限', key: 'limit', render: (_: any, r: AccountRow) => limitCell(r) },
    { title: <span title="本月已用来自 User Activity Report（日级延迟约 1 天）；80% 橙色、到上限红色">本月已用 / 上限</span>,
      key: 'used', align: 'right' as const, render: (_: any, r: AccountRow) => usedCell(r),
      defaultSortOrder: 'descend' as const,
      sorter: (a: AccountRow, b: AccountRow) => (a.month_credits ?? 0) - (b.month_credits ?? 0) },
    { title: '本月消息', dataIndex: 'month_messages', align: 'right' as const, render: fmtNum },
    { title: '最后活跃', dataIndex: 'usage_last_active', render: (v: string | null) => v || dash },
    { title: '操作', key: 'ops', render: (_: any, r: AccountRow) => (
      <Space size={4}>
        <Button size="small" onClick={() => setHistoryRow(r)}>用量</Button>
        <Button size="small" disabled={!isAccountActive(r)}
          onClick={() => setLimitTarget({ scope: 'USER', row: r, current: r.limit_scope === 'USER' ? r.limit : null })}>设上限</Button>
        {prepaid && r.live_synced && r.pool_status === 'AWAITING_SEAT' && (
          <Button size="small" onClick={() => doRetrySeat(r)}>分配席位</Button>
        )}
        {isAccountActive(r) ? (
          <Button size="small" danger onClick={() => openDeactivate([r])}>停用</Button>
        ) : (
          <Popconfirm title={`把 ${r.kiro_username} 重新加入积分池？${prepaid ? '需要有空余席位' : '后付池会自动占座计费'}`}
            onConfirm={() => doReactivate(r)}>
            <Button size="small" type="primary" ghost>恢复</Button>
          </Popconfirm>
        )}
      </Space>
    ) },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Row gutter={16}>
        <Col span={4}><Card size="small"><Statistic title="账号数（每人一个）" value={rows.length} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="积分池内" value={inPool.length} suffix={`/ ${rows.length}`} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="等待席位" value={awaiting.length}
          valueStyle={awaiting.length ? { color: '#d46b08' } : undefined} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="本月 Credits（全员）" value={totalCredits} precision={1} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="本月有用量的人" value={activeUsers} suffix={`/ ${rows.length}`} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="用到 80% 上限" value={nearLimit.length}
          valueStyle={nearLimit.length ? { color: '#d46b08' } : undefined} /></Card></Col>
      </Row>

      <Row gutter={16}>
        <Col span={14}>
          <Card size="small" title="Top 用户 · 本月 Credits"><HBarChart data={topByCredits} /></Card>
        </Col>
        <Col span={10}>
          <Card size="small" title="上限来源分布（积分池内账号）"><DonutChart data={scopeDist} /></Card>
        </Col>
      </Row>

      <DeactivateModal open={deactOpen} targets={deactTargets} summary={deactSummary}
        onClose={() => setDeactOpen(false)} onDone={() => { setSelectedKeys([]); load(true) }} />
      <UsageHistoryModal row={historyRow} onClose={() => setHistoryRow(null)} />
      <LimitModal target={limitTarget} onClose={() => setLimitTarget(null)} onDone={() => load(true)} />

      <Card size="small" title="账号列表（每人一个账号）"
        extra={<Button size="small" loading={loading} onClick={() => load(true)}>刷新</Button>}>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
          积分池状态与上限为 AWS 实时数据（在控制台直接移出 / 改上限也会反映）；用量来自 User Activity Report（约每日更新）。
          「停用」= 立即移出积分池；「设上限」即时生效。— 表示暂无数据，点「刷新」强制重查。
        </Typography.Paragraph>
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Space wrap size={8} data-testid="flat-filters">
            <Select mode="multiple" allowClear placeholder="分组" style={{ minWidth: 180 }} value={fTeams}
              onChange={setFTeams} options={allTeams.map((t) => ({ value: t, label: t }))} maxTagCount="responsive" />
            <Select allowClear placeholder="积分池状态" style={{ width: 130 }} value={fPool} onChange={setFPool}
              options={[{ value: 'SEATED', label: '已占座' }, { value: 'AWAITING_SEAT', label: '等待席位' }, { value: 'NONE', label: '不在池中' }]} />
            <Select allowClear placeholder="上限来源" style={{ width: 130 }} value={fScope} onChange={setFScope}
              options={[{ value: 'USER', label: '个人上限' }, { value: 'GROUP', label: '组上限' },
                        { value: 'POOL', label: 'profile 默认' }, { value: 'NONE', label: '无上限' }]} />
            <InputNumber placeholder="不限" style={{ width: 190 }} min={0} value={fCreditsLt}
              onChange={(v) => setFCreditsLt(v)} addonBefore="本月 Credits <" />
            <InputNumber placeholder="天" style={{ width: 150 }} min={1} value={fInactiveDays}
              onChange={(v) => setFInactiveDays(v)} addonBefore="未活跃 ≥" addonAfter="天" />
            <Button size="small" onClick={resetFilters}>清除筛选</Button>
            <span style={{ color: '#888', fontSize: 12 }}>匹配 {filtered.length} / {rows.length}</span>
          </Space>
          <Space size={8}>
            <span style={{ fontSize: 12, color: '#888' }}>快速选择：</span>
            <Button size="small" onClick={presetLowUsage}>
              长期不用的账号（本月 Credits &lt; {fCreditsLt ?? 50} 且 ≥ {fInactiveDays ?? 14} 天未活跃）
            </Button>
            <Button size="small" onClick={() => selectOf(filtered)}>选中当前匹配的账号</Button>
          </Space>
          <Table rowKey="kiro_user_id" dataSource={filtered} columns={cols} loading={loading} size="small"
            pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个账号` }}
            rowSelection={{
              selectedRowKeys: selectedKeys,
              onChange: (keys) => setSelectedKeys(keys as string[]),
              preserveSelectedRowKeys: true,
              getCheckboxProps: (r: AccountRow) => ({ disabled: !isAccountActive(r) }),
            }} />
          {selectedKeys.length > 0 && (
            <Alert type="warning" showIcon
              message={
                <Space size={12}>
                  <span>已选 <b>{selectedKeys.length}</b> 个账号（{selectedKeys.length} 人）</span>
                  <Button size="small" type="primary" danger
                    onClick={() => openDeactivate(selectedRows.filter(isAccountActive),
                      `来自列表勾选：${selectedRows.length} 个账号${filterDesc ? `（筛选：${filterDesc}）` : ''}`)}>
                    批量停用 / 回收
                  </Button>
                  <Button size="small" onClick={() => setSelectedKeys([])}>清空选择</Button>
                </Space>
              } />
          )}
        </Space>
      </Card>
    </Space>
  )
}

/** 积分池总览：余额、消耗速度、预计耗尽、席位、profile 默认上限、追加 credits、配额。 */
function PoolPanel({ info, loading, reload }: { info: PoolInfo | null; loading: boolean; reload: (force?: boolean) => void }) {
  const [limitOpen, setLimitOpen] = useState(false)
  const b = info?.balance
  const seats = info?.seats
  const quotas = info?.quotas
  const prepaid = !!info?.context?.prepaid
  const pct = (used: number, q: number) => (q > 0 ? Math.min(100, Math.round((used / q) * 100)) : 0)
  const runout = !b ? '—'
    : b.pool_runout_at ? `${fmtDate(b.pool_runout_at)}（本账期内耗尽）`
      : b.avg_daily_used > 0 ? '本账期够用' : '暂无消耗'
  const onboarding = quotas?.onboarding_cap

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {info && info.errors.length > 0 && (
        <Alert type="warning" showIcon message="部分数据暂不可用" description={info.errors.join('；')} />
      )}
      {info && !info.context && (
        <Alert type="error" showIcon message="无法读取积分池" description="请确认该账号的 Kiro profile 已开启 Credit sharing，且运行角色有 q:* / user-subscriptions:* 读权限。" />
      )}
      <Card size="small" title={
        <Space>
          积分池
          {info?.context && <Tag color={prepaid ? 'purple' : 'blue'}>{prepaid ? '预付（先买席位再分配）' : '后付（加人即自动占座）'}</Tag>}
          {b && <span style={{ fontWeight: 400, fontSize: 12, color: '#888' }}>本账期 {fmtDate(b.cycle_start)} ~ {fmtDate(b.cycle_end)}（UTC），剩 {b.days_left} 天</span>}
        </Space>}
        extra={<Button size="small" loading={loading} onClick={() => reload(true)}>刷新</Button>}>
        <Row gutter={16}>
          <Col span={6}>
            <Statistic title="池内剩余 credits" value={b ? b.total_left : '—'} precision={0}
              suffix={b ? `/ ${b.total_quantity.toLocaleString()}` : undefined} />
          </Col>
          <Col span={6}>
            <Statistic title="本月月度 credits（席位送的）" value={b ? b.monthly.used : '—'} precision={0}
              suffix={b ? `/ ${b.monthly.quantity.toLocaleString()} 已用` : undefined} />
            {b && <Progress percent={pct(b.monthly.used, b.monthly.quantity)} size="small"
              status={b.monthly.left === 0 ? 'exception' : undefined} />}
          </Col>
          <Col span={6}>
            <Statistic title="追加 credits 剩余" value={b ? b.addon.left : '—'} precision={0}
              suffix={b ? `/ ${b.addon.quantity.toLocaleString()}` : undefined} />
            {b && b.addon.expiring_this_cycle > 0 && (
              <Typography.Text type="warning" style={{ fontSize: 12 }}>本账期到期 {b.addon.expiring_this_cycle.toLocaleString()}</Typography.Text>
            )}
          </Col>
          <Col span={6}>
            <Statistic title="日均消耗 / 预计耗尽" value={b ? b.avg_daily_used : '—'} precision={1}
              suffix={b ? 'credits/天' : undefined} />
            <Typography.Text type={b?.pool_runout_at ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
              {runout}{b?.monthly_runout_at ? `；月度 credits 约 ${fmtDate(b.monthly_runout_at)} 用完，之后消耗追加 credits` : ''}
              {b?.rate_is_lower_bound ? '（月度已用完，速度为下限估计）' : ''}
            </Typography.Text>
          </Col>
        </Row>
      </Card>

      <Row gutter={16}>
        <Col span={12}>
          <Card size="small" title="席位">
            <Descriptions size="small" column={2}>
              <Descriptions.Item label="已占座">{fmtNum(seats?.seated)}</Descriptions.Item>
              <Descriptions.Item label="等待席位">
                <span style={seats?.awaiting ? { color: '#d46b08', fontWeight: 600 } : undefined}>{fmtNum(seats?.awaiting)}</span>
              </Descriptions.Item>
              {prepaid && <Descriptions.Item label="已购席位">{fmtNum(seats?.total)}</Descriptions.Item>}
              {prepaid && <Descriptions.Item label="空余席位">{fmtNum(seats?.available)}</Descriptions.Item>}
              {prepaid && seats?.pending_removal ? <Descriptions.Item label="待删除（下账期生效）">{seats.pending_removal}</Descriptions.Item> : null}
              <Descriptions.Item label="本账期计费席位">{b ? b.monthly.seat_count : '—'}</Descriptions.Item>
              <Descriptions.Item label="席位费（整月价）">{b ? `$${b.seat_fee_full_month_usd.toLocaleString()}` : '—'}</Descriptions.Item>
            </Descriptions>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
              每个席位 ${b?.seat_price_usd ?? 20}/月，向池里贡献 {b?.monthly.credits_per_seat ?? 500} credits/月；首月按天折算。
              {prepaid
                ? '预付池：没有空余席位时新开通的账号处于「等待席位」，在控制台买席位后到「账号总览」点「分配席位」。'
                : '后付池：加入即占座；可接入人数受 Service Quotas 上限约束。'}
            </Typography.Paragraph>
          </Card>
        </Col>
        <Col span={12}>
          <Card size="small" title="上限与配额">
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="profile 默认上限（每人每月）">
                <Space>
                  {info?.profile_limit != null ? `${info.profile_limit.toLocaleString()} credits` : <span>未设置（不限）</span>}
                  <Typography.Link onClick={() => setLimitOpen(true)} disabled={!info?.context}>修改</Typography.Link>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="可接入用户上限">
                {onboarding?.value != null
                  ? <span>{onboarding.value.toLocaleString()} 人（已占座 {seats?.seated ?? '—'}）　<a href={onboarding.console_url} target="_blank" rel="noreferrer">去 Service Quotas 提额</a></span>
                  : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="每月最多可买 credits">
                {quotas?.monthly_purchase_cap?.value != null ? quotas.monthly_purchase_cap.value.toLocaleString() : '—'}
              </Descriptions.Item>
            </Descriptions>
            <Alert type="info" showIcon style={{ marginTop: 8 }}
              message="买席位 / 买 credits 只能在 Kiro 控制台完成"
              description={<span>
                Kiro 控制台 → Pool and Access Management → Credit sharing →「Add seat」/「Buy more credits」（追加 credits 约 ${b?.addon_credit_price_usd ?? 0.04}/credit，12 个月有效，单次 125 起）。
                {info?.console_url && <> <a href={info.console_url} target="_blank" rel="noreferrer">打开控制台</a></>}
              </span>} />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="追加 credits 购买记录">
        <Table rowKey="id" size="small" pagination={{ pageSize: 5 }} dataSource={info?.purchases || []}
          locale={{ emptyText: '暂无购买记录' }}
          columns={[
            { title: '购买时间', dataIndex: 'created_at', render: fmtDate },
            { title: '数量（credits）', dataIndex: 'quantity', align: 'right' as const, render: fmtNum },
            { title: '来源', dataIndex: 'source', render: (s: string) => (s === 'AUTO_TOPUP' ? '自动充值' : '手动购买') },
            { title: '状态', dataIndex: 'status', render: (s: string) => <Tag color={s === 'ACTIVE' ? 'green' : 'default'}>{s}</Tag> },
            { title: '有效期至', dataIndex: 'expires_at', render: fmtDate },
            { title: '剩余', key: 'left', align: 'right' as const, render: (_: any, p: any) => {
              const item = b?.addon.items.find((x) => x.id === p.id)
              return item ? fmtCredits(item.left) : dash
            } },
          ]} />
      </Card>

      <LimitModal target={limitOpen ? { scope: 'POOL', current: info?.profile_limit ?? null } : null}
        onClose={() => setLimitOpen(false)} onDone={() => reload(true)} />
    </Space>
  )
}

function GroupsPanel() {
  const [groups, setGroups] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)

  const load = async () => {
    setLoading(true)
    try { setGroups(await getGroups()) }
    catch { message.error('加载失败') } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const doCreate = async () => {
    const name = newName.trim()
    if (!name) return
    setCreating(true)
    try {
      await createGroup(name)
      message.success(`分组 ${name} 已创建`)
      setNewName('')
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '创建失败')
    } finally { setCreating(false) }
  }

  return (
    <Card title="分组管理" extra={<Button size="small" onClick={load}>刷新</Button>}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        分组即 IAM Identity Center 的 Group（申请表单的分组下拉从这里拉取）。新建后用户申请账号时即可选择；开通时账号会加入该组。
        平台按「个人」把账号加入积分池；如果在 Kiro 控制台把整个组加入了积分池，组内成员会自动获得访问。
      </Typography.Paragraph>
      <Space style={{ marginBottom: 16 }}>
        <Input placeholder="新分组名，如 Team-Backend" value={newName} onChange={(e) => setNewName(e.target.value)}
          onPressEnter={doCreate} style={{ width: 280 }} />
        <Button type="primary" loading={creating} disabled={!newName.trim()} onClick={doCreate}>新建分组</Button>
      </Space>
      <Table rowKey={(g) => g} dataSource={groups} columns={[{ title: '分组名', render: (g: string) => g }]}
        loading={loading} size="small" pagination={{ pageSize: 15 }} />
    </Card>
  )
}

function ImportPanel() {
  const [rows, setRows] = useState<UnlinkedAccount[]>([])
  const [users, setUsers] = useState<FeishuUser[]>([])
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [picks, setPicks] = useState<Record<string, string>>({})
  const [linking, setLinking] = useState<string>('')

  // IDC 全量扫描有秒级耗时，不自动加载——管理员点「扫描」触发
  const scan = async () => {
    setLoading(true)
    try {
      const [u, f] = await Promise.all([getUnlinked(), getFeishuUsers()])
      setRows(u); setUsers(f); setLoaded(true)
    } catch { message.error('扫描失败') } finally { setLoading(false) }
  }

  const doLink = async (r: UnlinkedAccount) => {
    const openId = picks[r.kiro_user_id] || r.suggested_open_id
    if (!openId) return
    const name = users.find((u) => u.open_id === openId)?.name || r.suggested_name
    setLinking(r.kiro_user_id)
    try {
      await linkUnlinked(r.kiro_user_id, openId, name)
      message.success(`${r.kiro_username} 已绑定给 ${name}`)
      setRows((prev) => prev.filter((x) => x.kiro_user_id !== r.kiro_user_id))
    } catch (e: any) {
      message.error(e.response?.data?.detail || '绑定失败')
    } finally { setLinking('') }
  }

  const confTag = (c: string) =>
    c === 'email' ? <Tag color="green">邮箱匹配</Tag> : c === 'pinyin' ? <Tag color="orange">拼音匹配</Tag> : <Tag>无建议</Tag>

  const cols = [
    { title: 'IDC 用户名', dataIndex: 'kiro_username' },
    { title: '显示名', dataIndex: 'display_name' },
    { title: '邮箱', dataIndex: 'kiro_email' },
    { title: '建议归属', render: (_: any, r: UnlinkedAccount) => (
      <Space size={4}>{confTag(r.confidence)}{r.suggested_name && <span>{r.suggested_name}</span>}</Space>
    ) },
    { title: '绑定给', render: (_: any, r: UnlinkedAccount) => (
      <Select style={{ width: 180 }} placeholder="选择飞书用户" showSearch optionFilterProp="label"
        value={picks[r.kiro_user_id] || r.suggested_open_id || undefined}
        onChange={(v) => setPicks((p) => ({ ...p, [r.kiro_user_id]: v }))}
        options={users.map((u) => ({ value: u.open_id, label: u.has_account ? `${u.name}（已有账号）` : u.name,
                                     disabled: !!u.has_account }))} />
    ) },
    { title: '操作', render: (_: any, r: UnlinkedAccount) => {
      const target = picks[r.kiro_user_id] || r.suggested_open_id
      const name = users.find((u) => u.open_id === target)?.name || r.suggested_name
      return (
        <Popconfirm title={`确认把 ${r.kiro_username} 绑定给 ${name || '?'}？`} onConfirm={() => doLink(r)} disabled={!target}>
          <Button size="small" type="primary" disabled={!target} loading={linking === r.kiro_user_id}>绑定</Button>
        </Popconfirm>
      )
    } },
  ]

  return (
    <Card title="存量账号导入"
      extra={<Button type="primary" loading={loading} onClick={scan}>{loaded ? '重新扫描' : '扫描游离账号'}</Button>}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        游离账号 = IDC 里存在但未绑定飞书用户的账号（非本平台开通的存量）。
        绑定后账号会出现在对方的「我的 Kiro 账号」，积分池状态 / 上限 / 用量自动按实况显示。
        建议归属：<Tag color="green">邮箱匹配</Tag>可信度高；<Tag color="orange">拼音匹配</Tag>请人工核对后再绑定。
        每人只能绑定一个账号，已有账号的飞书用户不可选；绑定目标下拉仅含在平台登录 / 申请过的飞书用户。
      </Typography.Paragraph>
      {!loaded && !loading && <Alert type="info" showIcon message="点右上角「扫描游离账号」开始（全量扫 IDC，数秒）" />}
      {loaded && (
        <Table rowKey="kiro_user_id" dataSource={rows} columns={cols} loading={loading} size="small"
          pagination={{ pageSize: 15 }} locale={{ emptyText: '没有游离账号，全部已关联 ✅' }} />
      )}
    </Card>
  )
}

export default function AdminPage() {
  const nav = useNavigate()
  const [pool, setPool] = useState<PoolInfo | null>(null)
  const [poolLoading, setPoolLoading] = useState(false)
  const loadPool = async (force = false) => {
    setPoolLoading(true)
    try { setPool(await getPool(force)) }
    catch { setPool(null) } finally { setPoolLoading(false) }
  }
  useEffect(() => { loadPool() }, [])
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Title level={4} style={{ margin: 0 }}>管理后台</Title>
        <Space>
          {pool?.balance && (
            <span style={{ fontSize: 13, color: '#555' }}>
              积分池剩余 <b>{Math.round(pool.balance.total_left).toLocaleString()}</b> credits
              {pool.balance.pool_runout_at && <Tag color="red" style={{ marginLeft: 6 }}>预计 {fmtDate(pool.balance.pool_runout_at)} 耗尽</Tag>}
            </span>
          )}
          <Button onClick={() => nav('/')}>返回</Button>
        </Space>
      </Header>
      <Content style={{ padding: 24 }}>
        <Tabs
          defaultActiveKey="requests"
          items={[
            { key: 'requests', label: '申请审批', children: <RequestsPanel /> },
            { key: 'pool', label: '积分池', children: <PoolPanel info={pool} loading={poolLoading} reload={loadPool} /> },
            { key: 'accounts', label: '账号总览', children: <AccountsPanel prepaid={!!pool?.context?.prepaid} /> },
            { key: 'groups', label: '分组管理', children: <GroupsPanel /> },
            { key: 'import', label: '存量导入', children: <ImportPanel /> },
          ]}
        />
      </Content>
    </Layout>
  )
}
