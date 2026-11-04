// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Table, Tag, Space, Segmented, message, Popconfirm,
  Typography, Layout, Tabs, Row, Col, Statistic, Modal, InputNumber, Alert, Input, Select,
  Radio, Checkbox,
} from 'antd'
import {
  adminRequests, approve, reject, getAccounts, getOverageCap, raiseOverageCap,
  getGroups, createGroup, getUnlinked, getFeishuUsers, linkUnlinked, deactivateAccounts, reactivateAccounts,
  getBillingHistory,
  ReqItem, AccountRow, OverageCapInfo, UnlinkedAccount, FeishuUser, DeletionMode, DeactivateResult, BillingHistory,
} from '../api'
import { HBarChart, DonutChart } from '../components/MiniCharts'

const { Header, Content } = Layout
const { Title } = Typography

const TYPE_LABEL: any = {
  apply: '开通', upgrade: '升级', quota_increase: '配额', overage_cap: '超额上限',
  deactivate: '停用', recycle: '回收', reactivate: '恢复订阅',
}
const STATUS_COLOR: any = {
  pending: 'orange', approved: 'blue', executed: 'green', failed: 'red', rejected: 'default',
}

// 用量空值（Athena 未配置/无数据）统一显示 —
const fmtNum = (v: number | null) =>
  v === null || v === undefined ? <span style={{ color: '#ccc' }}>—</span> : v.toLocaleString()
const fmtCredits = (v: number | null) =>
  v === null || v === undefined ? <span style={{ color: '#ccc' }}>—</span> : v.toFixed(2)
const fmtMoney = (v: number | null, cur = 'USD') =>
  v === null || v === undefined
    ? <span style={{ color: '#ccc' }}>—</span>
    : <span>{cur === 'USD' ? '$' : `${cur} `}{v.toFixed(2)}</span>
// 套餐代码 → 展示名（KIRO_ENTERPRISE_PRO_MAX → Pro Max）
const planLabel = (code: string | null) => {
  if (!code) return '—'
  const m: Record<string, string> = {
    KIRO_ENTERPRISE_PRO: 'Pro', KIRO_ENTERPRISE_PRO_PLUS: 'Pro+',
    KIRO_ENTERPRISE_PRO_MAX: 'Pro Max', KIRO_ENTERPRISE_POWER: 'Power',
  }
  return m[code] || code
}

/** 历史月账单弹窗：出账对账 / 计费争议时按月回溯某账号的额度、用量、超额。 */
function BillingHistoryModal({ row, onClose }: { row: AccountRow | null; onClose: () => void }) {
  const now = new Date()
  const [year, setYear] = useState(now.getUTCFullYear())
  const [month, setMonth] = useState(now.getUTCMonth() + 1)
  const [data, setData] = useState<BillingHistory | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const query = async (y: number, m: number) => {
    if (!row) return
    setLoading(true); setErr(''); setData(null)
    try { setData(await getBillingHistory(row.kiro_user_id, y, m)) }
    catch (e: any) { setErr(e.response?.data?.detail || '查询失败') }
    finally { setLoading(false) }
  }
  useEffect(() => {
    if (row) { setYear(now.getUTCFullYear()); setMonth(now.getUTCMonth() + 1); query(now.getUTCFullYear(), now.getUTCMonth() + 1) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row])

  const years = [now.getUTCFullYear() - 1, now.getUTCFullYear()]
  return (
    <Modal title={`账单回溯 · ${row?.kiro_username || ''}`} open={!!row} onCancel={onClose} footer={null} width={560}>
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
            description="账单回溯依赖 Kiro Console 的 User Activity Report（Athena）。请确认已开启报表并配置 Athena。" />
        )}
        {data && data.available && !data.has_usage_entry && (
          <Alert type="info" showIcon message={`${year} 年 ${month} 月无该账号的用量记录`}
            description="该月没有任何活动，或报表尚未沉淀该月数据（日级延迟约 1 天）。" />
        )}
        {data && data.has_usage_entry && (
          <Row gutter={12}>
            <Col span={6}><Card size="small"><Statistic title="已用 credits" value={data.credit_used ?? '—'} precision={1} /></Card></Col>
            <Col span={6}><Card size="small"><Statistic title="超额 credits" value={data.overage_credits ?? '—'} precision={1}
              valueStyle={data.overage_credits ? { color: '#cf1322' } : undefined} /></Card></Col>
            <Col span={6}><Card size="small"><Statistic title="预估超额费用" prefix="$" value={data.overage_charges_est ?? '—'} precision={2} /></Card></Col>
            <Col span={6}><Card size="small"><Statistic title="活跃天数" value={data.active_days ?? '—'} suffix="天" />
              {data.messages != null && <Typography.Text type="secondary" style={{ fontSize: 12 }}>{data.messages.toLocaleString()} 条消息</Typography.Text>}
            </Card></Col>
          </Row>
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          用量来自 Kiro Console 的 User Activity Report（活动口径，按 UTC 自然月，日级延迟约 1 天），与 Kiro 控制台 Dashboard 一致；
          预估超额费用 = 超额 credits × 单价 {data?.overage_rate != null ? `$${data.overage_rate}/credit` : '—'}
          {data?.credit_limit != null ? `；当前套餐月额度 ${data.credit_limit.toLocaleString()}（${planLabel(data.plan_code)}）` : ''}。
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

// 有可停用的订阅：实况优先；实况不可得时按快照
const hasLiveSubscription = (r: AccountRow) =>
  r.live_synced ? r.live_status != null && r.live_status !== 'CANCELLED' : r.status === 'active'

const MODE_LABEL: Record<DeletionMode, string> = { end_of_period: '月底停用', immediate: '立即停用' }

/** 停用 / 回收弹窗：复刻控制台「Deactivate Kiro plan」的 End of the month / Immediately 两个选项。 */
const BATCH_LIMIT = 200           // 后端单次上限；超出前端自动分批
const CONFIRM_TYPE_THRESHOLD = 10 // 名单 ≥ 此数时，确认须手动输入账号数

const downloadCsv = (filename: string, header: string[], rows: (string | number | null | undefined)[][]) => {
  const esc = (v: any) => `"${String(v ?? '').replace(/"/g, '""')}"`
  const csv = '﻿' + [header, ...rows].map((r) => r.map(esc).join(',')).join('\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}

/** 停用 / 回收弹窗 = 「核对名单 + 选择方式 + 确认」。选人在明细表完成，弹窗不再提供补选。 */
function DeactivateModal({ open, targets, summary, onClose, onDone }: {
  open: boolean
  targets: AccountRow[]            // 待停用名单（单行操作传 1 个；批量来自明细视图勾选）
  summary?: string                 // 来源摘要（如「来自明细视图勾选：120 个账号（筛选：分组 Team-A · 副账号）」）
  onClose: () => void
  onDone: () => void
}) {
  const [rows, setRows] = useState<AccountRow[]>([])
  const [mode, setMode] = useState<DeletionMode>('end_of_period')
  const [recycle, setRecycle] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [results, setResults] = useState<DeactivateResult[] | null>(null)
  const [kw, setKw] = useState('')
  const [primaryOnly, setPrimaryOnly] = useState(false)
  const [typed, setTyped] = useState('')

  useEffect(() => {
    if (open) {
      setRows(targets); setMode('end_of_period'); setRecycle(false); setResults(null)
      setKw(''); setPrimaryOnly(false); setTyped('')
    }
  }, [open, targets])

  const n = rows.length
  const primaryRows = rows.filter((r) => r.account_role === 'primary')
  const byTeam = (() => {
    const m = new Map<string, number>()
    for (const r of rows) { const t = r.team || '（无分组）'; m.set(t, (m.get(t) || 0) + 1) }
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  })()
  const byTier = (() => {
    const m = new Map<string, number>()
    for (const r of rows) { const t = (r.live_synced ? r.live_tier : r.tier) || '—'; m.set(t, (m.get(t) || 0) + 1) }
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  })()
  const subsTotal = rows.reduce((s, r) => s + (r.billing_current_charges ?? 0), 0)
  const hasBilling = rows.some((r) => r.billing_current_charges != null)

  const shown = rows.filter((r) =>
    (!primaryOnly || r.account_role === 'primary') &&
    (!kw || `${r.kiro_username} ${r.feishu_name} ${r.team}`.toLowerCase().includes(kw.toLowerCase())))
  const remove = (id: string) => setRows((cur) => cur.filter((r) => r.kiro_user_id !== id))

  const needType = n >= CONFIRM_TYPE_THRESHOLD
  const typedOk = !needType || typed.trim() === String(n)

  const submit = async () => {
    if (!n) return
    setSubmitting(true)
    try {
      const ids = rows.map((r) => r.kiro_user_id)
      const all: DeactivateResult[] = []
      for (let i = 0; i < ids.length; i += BATCH_LIMIT) {
        const resp = await deactivateAccounts(ids.slice(i, i + BATCH_LIMIT), mode, recycle)
        all.push(...resp.results)
      }
      const succeeded = all.filter((r) => r.success).length
      const failed = all.length - succeeded
      setResults(all)
      if (failed === 0) message.success(`${recycle ? '回收' : MODE_LABEL[mode]}完成：${succeeded} 个账号`)
      else message.warning(`完成 ${succeeded} 个，失败 ${failed} 个，详见下方`)
      onDone()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '提交失败')
    } finally { setSubmitting(false) }
  }

  const scope = byTeam.length === 1 ? `分组 ${byTeam[0][0]} 的 ` : byTeam.length > 1 ? `${byTeam.length} 个分组共 ` : ''
  const actionLabel = recycle ? '回收' : MODE_LABEL[mode]
  const confirmTitle = recycle
    ? `确认回收${scope}${n} 个账号？退订 + 删除 IDC 用户 + 解除映射，不可恢复`
    : mode === 'immediate'
      ? `确认立即停用${scope}${n} 个账号？用户马上失去访问，当月仍整月计费`
      : `确认月底停用${scope}${n} 个账号？用户保留访问到月底`

  const exportResults = () => results && downloadCsv(
    `kiro-${recycle ? 'recycle' : 'deactivate'}-${mode}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`,
    ['username', 'user_id', 'success', 'error', 'mode', 'recycle'],
    results.map((r) => [r.username, r.user_id, r.success ? 'success' : 'failed', r.error, mode, recycle ? 'yes' : 'no']))
  const exportList = () => downloadCsv(
    `kiro-deactivate-list-${new Date().toISOString().slice(0, 10)}.csv`,
    ['username', 'feishu_name', 'role', 'team', 'tier', 'current_charges', 'credits'],
    rows.map((r) => [r.kiro_username, r.feishu_name, r.account_role, r.team,
      (r.live_synced ? r.live_tier : r.tier) || '', r.billing_current_charges, r.usage_credits]))

  const reviewCols = [
    { title: '用户名', dataIndex: 'kiro_username', sorter: (a: AccountRow, b: AccountRow) => a.kiro_username.localeCompare(b.kiro_username) },
    { title: '飞书用户', dataIndex: 'feishu_name', render: (v: string) => v || '—' },
    { title: '主/副', dataIndex: 'account_role', width: 64, render: roleTag,
      sorter: (a: AccountRow, b: AccountRow) => a.account_role.localeCompare(b.account_role) },
    { title: '分组', dataIndex: 'team', render: (v: string) => v || '—',
      sorter: (a: AccountRow, b: AccountRow) => (a.team || '').localeCompare(b.team || '') },
    { title: 'Tier', key: 'tier', render: (_: any, r: AccountRow) => (r.live_synced ? r.live_tier : r.tier) || '—' },
    { title: '本月费用', dataIndex: 'billing_current_charges', align: 'right' as const,
      render: (v: number | null, r: AccountRow) => fmtMoney(v, r.billing_currency),
      sorter: (a: AccountRow, b: AccountRow) => (a.billing_current_charges ?? 0) - (b.billing_current_charges ?? 0) },
    { title: 'Credits', dataIndex: 'usage_credits', align: 'right' as const, render: fmtCredits,
      sorter: (a: AccountRow, b: AccountRow) => (a.usage_credits ?? 0) - (b.usage_credits ?? 0) },
    { title: '', key: 'rm', width: 56, render: (_: any, r: AccountRow) => (
      <Button size="small" type="text" danger onClick={() => remove(r.kiro_user_id)} aria-label={`移除 ${r.kiro_username}`}>移除</Button>
    ) },
  ]

  return (
    <Modal
      title={recycle ? '回收账号' : '停用 Kiro 订阅'}
      open={open}
      onCancel={onClose}
      width={860}
      footer={results ? [
        <Button key="csv" onClick={exportResults}>导出结果 CSV</Button>,
        <Button key="close" type="primary" onClick={onClose}>关闭</Button>,
      ] : [
        <Button key="cancel" onClick={onClose}>取消</Button>,
        <Popconfirm key="ok" title={confirmTitle} onConfirm={submit} disabled={!n || !typedOk}>
          <Button type="primary" danger={recycle || mode === 'immediate'} loading={submitting} disabled={!n || !typedOk}>
            {recycle ? '确认回收' : '确认停用'}{n ? `（${n}）` : ''}
          </Button>
        </Popconfirm>,
      ]}
    >
      {results ? (
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Alert type={results.every((r) => r.success) ? 'success' : 'warning'} showIcon
            message={`${actionLabel}完成：成功 ${results.filter((r) => r.success).length} / 失败 ${results.filter((r) => !r.success).length}`} />
          <Table rowKey="user_id" dataSource={results} size="small" pagination={{ pageSize: 10 }}
            columns={[
              { title: '用户名', dataIndex: 'username', render: (v: string, r: DeactivateResult) => v || r.user_id },
              { title: '结果', dataIndex: 'success', width: 80,
                render: (ok: boolean) => ok ? <Tag color="green">成功</Tag> : <Tag color="red">失败</Tag>,
                sorter: (a: DeactivateResult, b: DeactivateResult) => Number(a.success) - Number(b.success) },
              { title: '说明', dataIndex: 'error', render: (e: string) => e || '—' },
            ]} />
        </Space>
      ) : (
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          {summary && <Alert type="info" showIcon message={summary} />}

          {/* 汇总条：不翻页就能知道范围 */}
          <Card size="small" data-testid="deact-summary">
            <Space wrap size={[16, 6]}>
              <span>共 <b style={{ fontSize: 16 }}>{n}</b> 个账号</span>
              <span>分组：{byTeam.length ? byTeam.map(([t, c]) => `${t} ${c}`).join(' · ') : '—'}</span>
              <span>Tier：{byTier.map(([t, c]) => `${t} ${c}`).join(' · ') || '—'}</span>
              <span style={primaryRows.length ? { color: '#cf1322', fontWeight: 600 } : undefined}>
                主账号 {primaryRows.length}
              </span>
              {hasBilling && <span>本月订阅费合计 <b>${subsTotal.toFixed(2)}</b></span>}
              <Typography.Link onClick={exportList} style={{ fontSize: 12 }}>导出名单</Typography.Link>
            </Space>
          </Card>
          {primaryRows.length > 0 && (
            <Alert type="error" showIcon
              message={<span>名单里有 <b>{primaryRows.length}</b> 个主账号</span>}
              description="主账号是用户的永久账号，预设默认不选主账号。请在下方用「仅看主账号」核对，误选的点「移除」。" />
          )}

          {/* 核对名单：分页 + 排序 + 过滤 + 逐行移除；要加人请关掉弹窗回明细表勾选 */}
          <div>
            <Space style={{ marginBottom: 8 }} wrap>
              <Input.Search allowClear placeholder="按用户名 / 飞书用户 / 分组过滤" style={{ width: 280 }}
                value={kw} onChange={(e) => setKw(e.target.value)} />
              <Checkbox checked={primaryOnly} onChange={(e) => setPrimaryOnly(e.target.checked)}>仅看主账号</Checkbox>
              <span style={{ fontSize: 12, color: '#888' }}>显示 {shown.length} / {n}；要补选请关闭弹窗回明细表勾选</span>
            </Space>
            <Table rowKey="kiro_user_id" dataSource={shown} columns={reviewCols} size="small"
              pagination={{ pageSize: 10, size: 'small', showTotal: (t) => `共 ${t} 行` }}
              locale={{ emptyText: n ? '无匹配' : '名单为空' }} />
          </div>

          <div>
            <div style={{ marginBottom: 6 }}>停用时机</div>
            <Radio.Group value={mode} onChange={(e) => setMode(e.target.value)} style={{ width: '100%' }}>
              <Space direction="vertical" size={8}>
                <Radio value="end_of_period">
                  <b>月底停用（End of the month）</b>
                  <div style={{ color: '#888', fontSize: 12 }}>
                    用户保留 Kiro 访问到本月底，订阅在此期间显示 CANCELLED；月底前重新订阅可撤销停用。
                  </div>
                </Radio>
                <Radio value="immediate">
                  <b>立即停用（Immediately）</b>
                  <div style={{ color: '#888', fontSize: 12 }}>
                    用户立刻失去 Kiro 全部功能的访问。<b>当月费用仍整月计收，不会按比例退费</b>；适合离职、安全事件等需要马上断权限的场景。
                  </div>
                </Radio>
              </Space>
            </Radio.Group>
          </div>

          <Checkbox checked={recycle} onChange={(e) => setRecycle(e.target.checked)}>
            同时删除 IDC 用户并解除飞书映射（回收账号，<b>不可恢复</b>）
          </Checkbox>
          {recycle && (
            <Alert type="error" showIcon message="回收会删除 IAM Identity Center 用户"
              description="用户的登录身份随即消失，无法再登录 Kiro；通常只对低用量副账号执行。建议选「立即停用」，月底停用对已删除的用户没有意义。" />
          )}
          {!recycle && mode === 'immediate' && (
            <Alert type="warning" showIcon message="立即停用不省当月费用"
              description="AWS 口径：Immediately 只是立刻切断访问，账单仍收到账期结束。若目的是省钱，选「月底停用」即可。" />
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

function AccountsPanel() {
  const [rows, setRows] = useState<AccountRow[]>([])
  const [loading, setLoading] = useState(false)
  const [capInfo, setCapInfo] = useState<OverageCapInfo>({ cap: null, pending: null })
  const [capModalOpen, setCapModalOpen] = useState(false)
  const [capTarget, setCapTarget] = useState<number | null>(null)
  const [capSubmitting, setCapSubmitting] = useState(false)
  const [deactOpen, setDeactOpen] = useState(false)
  const [deactTargets, setDeactTargets] = useState<AccountRow[]>([])
  const [historyRow, setHistoryRow] = useState<AccountRow | null>(null)
  const [deactSummary, setDeactSummary] = useState<string | undefined>(undefined)
  // 明细视图：筛选 + 勾选
  const [view, setView] = useState<'person' | 'flat'>('person')
  const [fTeams, setFTeams] = useState<string[]>([])
  const [fTiers, setFTiers] = useState<string[]>([])
  const [fRole, setFRole] = useState<string | undefined>(undefined)
  const [fStatus, setFStatus] = useState<string | undefined>(undefined)
  const [fCreditsLt, setFCreditsLt] = useState<number | null>(null)     // null = 不限
  const [fInactiveDays, setFInactiveDays] = useState<number | null>(null)
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])

  const openDeactivate = (targets: AccountRow[], summary?: string) => {
    setDeactTargets(targets); setDeactSummary(summary); setDeactOpen(true)
  }
  const doReactivate = async (r: AccountRow) => {
    try {
      const resp = await reactivateAccounts([r.kiro_user_id])
      const one = resp.results[0]
      if (one?.success) message.success(`已恢复 ${r.kiro_username} 的订阅（回到 PENDING，用户登录后激活）`)
      else message.error(one?.error || '恢复失败')
      load(true)
    } catch (e: any) { message.error(e.response?.data?.detail || '恢复失败') }
  }
  const deactCandidates = rows.filter(hasLiveSubscription)

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
  // 本月预估账单 = Σ 本周期订阅费 + Σ 超额费用（计费口径，直读订阅计费服务）
  const billingRows = rows.filter((r) => r.billing_synced)
  const billSubs = billingRows.reduce((s, r) => s + (r.billing_current_charges ?? 0), 0)
  const billOver = billingRows.reduce((s, r) => s + (r.billing_overage_charges ?? 0), 0)
  const billMissing = rows.length - billingRows.length

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

  // ── 明细视图：筛选 ──
  const effTier = (r: AccountRow) => (r.live_synced ? (r.live_tier || '') : (r.tier || ''))
  const effStatus = (r: AccountRow) => (r.live_synced ? (r.live_status || 'NONE') : (r.status === 'active' ? 'ACTIVE' : 'NONE'))
  const daysSince = (d: string | null) => d ? Math.floor((Date.now() - new Date(d).getTime()) / 86400000) : Infinity
  const allTeams = [...new Set(rows.map((r) => r.team).filter(Boolean))].sort()
  const allTiers = [...new Set(rows.map(effTier).filter(Boolean))].sort()
  const filtered = rows.filter((r) =>
    (!fTeams.length || fTeams.includes(r.team)) &&
    (!fTiers.length || fTiers.includes(effTier(r))) &&
    (!fRole || r.account_role === fRole) &&
    (!fStatus || effStatus(r) === fStatus) &&
    (fCreditsLt == null || (r.usage_credits ?? 0) < fCreditsLt) &&
    (fInactiveDays == null || daysSince(r.usage_last_active) >= fInactiveDays))
  const filterDesc = [
    fTeams.length ? `分组 ${fTeams.join('/')}` : '',
    fTiers.length ? `Tier ${fTiers.join('/')}` : '',
    fRole ? (fRole === 'primary' ? '主账号' : '副账号') : '',
    fStatus ? `状态 ${fStatus}` : '',
    fCreditsLt != null ? `Credits < ${fCreditsLt}` : '',
    fInactiveDays != null ? `${fInactiveDays} 天未活跃` : '',
  ].filter(Boolean).join(' · ')
  const resetFilters = () => { setFTeams([]); setFTiers([]); setFRole(undefined); setFStatus(undefined); setFCreditsLt(null); setFInactiveDays(null) }
  // 预设只选副账号 + 有有效订阅（主账号须手动勾选，弹窗红字提示）
  const selectSecondaryOf = (list: AccountRow[]) =>
    setSelectedKeys(list.filter((r) => r.account_role === 'secondary' && hasLiveSubscription(r)).map((r) => r.kiro_user_id))
  const presetLowUsage = () => {
    setFRole('secondary'); setFCreditsLt(fCreditsLt ?? 50)
    const th = fCreditsLt ?? 50
    selectSecondaryOf(rows.filter((r) =>
      (!fTeams.length || fTeams.includes(r.team)) && (r.usage_credits ?? 0) < th))
  }
  const presetWholeTeam = () => {
    if (!fTeams.length) { message.info('先在「分组」里选择一个或多个组'); return }
    selectSecondaryOf(filtered)
  }
  const selectedRows = rows.filter((r) => selectedKeys.includes(r.kiro_user_id))
  const selectedPrimary = selectedRows.filter((r) => r.account_role === 'primary').length
  const openBatchFromSelection = () => {
    openDeactivate(selectedRows.filter(hasLiveSubscription),
      `来自明细视图勾选：${selectedRows.length} 个账号${filterDesc ? `（筛选：${filterDesc}）` : ''}`)
  }

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
    { title: '本月费用', dataIndex: 'billing_current_charges', align: 'right' as const,
      render: (v: number | null, r: AccountRow) => (
        <span title={r.billing_synced ? `套餐 ${planLabel(r.billing_plan_code)}${r.billing_plan_start ? ` · 自 ${r.billing_plan_start}` : ''}` : r.billing_error}>
          {fmtMoney(v, r.billing_currency)}
          {r.billing_pending_plan && <Tag color="orange" style={{ marginLeft: 4, fontSize: 11 }}>→ {planLabel(r.billing_pending_plan)}</Tag>}
          {r.billing_account_status && r.billing_account_status !== 'ACTIVE' && <Tag color="red" style={{ marginLeft: 4, fontSize: 11 }}>{r.billing_account_status}</Tag>}
        </span>
      ) },
    { title: <span title="本月已用 credits 来自 User Activity Report（活动口径，日级延迟约 1 天）；额度来自订阅计费服务">本月已用 / 额度</span>,
      key: 'billing_credit', align: 'right' as const,
      render: (_: any, r: AccountRow) => {
        if (r.billing_credit_limit == null) return <span style={{ color: '#ccc' }}>—</span>
        if (r.billing_credit_used == null) {
          return <span style={{ color: '#ccc' }} title="用量报表不可用">— / {r.billing_credit_limit.toLocaleString()}</span>
        }
        const used = r.billing_credit_used
        const pct = r.billing_credit_limit > 0 ? used / r.billing_credit_limit : 0
        const color = pct >= 1 ? '#cf1322' : pct >= 0.8 ? '#d46b08' : undefined
        return <span style={{ color }}>{used.toLocaleString(undefined, { maximumFractionDigits: 1 })} / {r.billing_credit_limit.toLocaleString()}</span>
      } },
    { title: <span title="预估 = 本月超额 credits × 超额单价（报表活动口径）">超额（预估）</span>,
      dataIndex: 'billing_overage_charges', align: 'right' as const,
      render: (v: number | null, r: AccountRow) => (
        <span style={{ color: v ? '#cf1322' : undefined }}
          title={r.billing_overage_credits != null ? `超额 ${r.billing_overage_credits} credits × $${r.billing_overage_rate ?? 0.04}` : undefined}>
          {fmtMoney(v, r.billing_currency)}
        </span>
      ) },
    { title: 'Credits', dataIndex: 'usage_credits', align: 'right' as const, render: fmtCredits },
    { title: '消息数', dataIndex: 'usage_messages', align: 'right' as const, render: fmtNum },
    { title: '最后活跃', dataIndex: 'usage_last_active',
      render: (v: string | null) => v || <span style={{ color: '#ccc' }}>—</span> },
    { title: '操作', key: 'ops', render: (_: any, r: AccountRow) => (
      <Space size={4}>
      <Button size="small" onClick={() => setHistoryRow(r)}>账单</Button>
      {r.live_synced && r.live_status === 'CANCELLED' ? (
        <Popconfirm title={`恢复 ${r.kiro_username} 的订阅（按原套餐重新订阅，撤销停用）？`}
          onConfirm={() => doReactivate(r)}>
          <Button size="small" type="primary" ghost>恢复</Button>
        </Popconfirm>
      ) : (
        <Button size="small" danger disabled={!hasLiveSubscription(r)}
          onClick={() => openDeactivate([r])}>停用</Button>
      )}
      </Space>
    ) },
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

  // ── 明细视图列 = 飞书用户 + 子账号列 ──
  const flatCols = [
    { title: '飞书用户', dataIndex: 'feishu_name', render: (v: string) => v || '—' },
    ...subCols,
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {/* 指标卡 */}
      <Row gutter={16}>
        <Col span={3}><Card size="small"><Statistic title="飞书用户数" value={totalPeople} /></Card></Col>
        <Col span={3}><Card size="small"><Statistic title="账号总数" value={totalAccounts} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="总 Credits" value={totalCredits} precision={1} /></Card></Col>
        <Col span={3}><Card size="small"><Statistic title="有用量的人" value={activePeople} suffix={`/ ${totalPeople}`} /></Card></Col>
        <Col span={5}>
          <Card size="small">
            <Statistic title="本月预估账单" prefix="$" value={billingRows.length ? billSubs + billOver : '—'} precision={2}
              valueStyle={billingRows.length ? undefined : { color: '#ccc' }} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {billingRows.length
                ? <>订阅 ${billSubs.toFixed(2)} + 超额 ${billOver.toFixed(2)}{billMissing > 0 && <>；{billMissing} 个账号无计费数据</>}</>
                : '计费服务暂不可用'}
            </Typography.Text>
          </Card>
        </Col>
        <Col span={6}>
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
      <DeactivateModal open={deactOpen} targets={deactTargets} summary={deactSummary}
        onClose={() => setDeactOpen(false)} onDone={() => { setSelectedKeys([]); load(true) }} />
      <BillingHistoryModal row={historyRow} onClose={() => setHistoryRow(null)} />

      <Card size="small"
        title={
          <Space size={12}>
            <Segmented size="small" value={view} onChange={(v) => setView(v as 'person' | 'flat')}
              options={[{ label: '按人聚合', value: 'person' }, { label: '账号明细（筛选 / 勾选）', value: 'flat' }]} />
            <span style={{ fontWeight: 400, fontSize: 12, color: '#888' }}>
              {view === 'person' ? '展开看名下各账号' : '按分组 / Tier / 用量筛选，勾选后批量停用'}
            </span>
          </Space>
        }
        extra={
          <Space size={8}>
            {view === 'person' && (
              <Button size="small" danger disabled={!deactCandidates.length}
                onClick={() => { setView('flat'); message.info('批量操作请在「账号明细」里筛选并勾选账号') }}>
                批量停用 / 回收
              </Button>
            )}
            <Button size="small" loading={loading} onClick={() => load(true)}>刷新</Button>
          </Space>
        }>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
          一人多账号已合并为一行，Credits/消息为名下所有账号汇总；点行首 ▸ 展开看每个子账号。
          Tier/订阅状态为 AWS 实时数据（控制台的退订/改套餐也会反映）；
          用量来自 Kiro Analytics（Athena，约每日更新），点「刷新」强制重查。— 表示暂无数据。
          「本月费用」与月额度直读 Kiro 订阅计费服务（实时）；「本月已用 / 超额（预估）」与 Credits/消息数来自 User Activity Report（活动口径，日级延迟约 1 天）；点「账单」可按月回溯。
          子账号行的「停用」= 控制台 Deactivate Kiro plan（可选月底 / 立即），已停用（CANCELLED）的行显示「恢复」可撤销；「批量停用 / 回收」可一次处理多个低用量副账号。
        </Typography.Paragraph>
        {view === 'person' ? (
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
        ) : (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {/* 筛选栏 */}
            <Space wrap size={8} data-testid="flat-filters">
              <Select mode="multiple" allowClear placeholder="分组" style={{ minWidth: 180 }} value={fTeams}
                onChange={setFTeams} options={allTeams.map((t) => ({ value: t, label: t }))} maxTagCount="responsive" />
              <Select mode="multiple" allowClear placeholder="Tier" style={{ minWidth: 150 }} value={fTiers}
                onChange={setFTiers} options={allTiers.map((t) => ({ value: t, label: t }))} maxTagCount="responsive" />
              <Select allowClear placeholder="主/副" style={{ width: 100 }} value={fRole} onChange={setFRole}
                options={[{ value: 'primary', label: '主账号' }, { value: 'secondary', label: '副账号' }]} />
              <Select allowClear placeholder="订阅状态" style={{ width: 130 }} value={fStatus} onChange={setFStatus}
                options={[{ value: 'ACTIVE', label: 'ACTIVE' }, { value: 'PENDING', label: 'PENDING' },
                          { value: 'CANCELLED', label: 'CANCELLED' }, { value: 'NONE', label: '无订阅' }]} />
              <InputNumber placeholder="不限" style={{ width: 170 }} min={0} value={fCreditsLt}
                onChange={(v) => setFCreditsLt(v)} addonBefore="Credits <" />
              <InputNumber placeholder="天" style={{ width: 150 }} min={1} value={fInactiveDays}
                onChange={(v) => setFInactiveDays(v)} addonBefore="未活跃 ≥" addonAfter="天" />
              <Button size="small" onClick={resetFilters}>清除筛选</Button>
              <span style={{ color: '#888', fontSize: 12 }}>匹配 {filtered.length} / {rows.length}</span>
            </Space>
            {/* 预设 */}
            <Space size={8}>
              <span style={{ fontSize: 12, color: '#888' }}>快速选择：</span>
              <Button size="small" onClick={presetLowUsage}>低用量副账号（Credits &lt; {fCreditsLt ?? 50}）</Button>
              <Button size="small" onClick={presetWholeTeam} disabled={!fTeams.length}>选中本组全部副账号</Button>
              <Button size="small" onClick={() => selectSecondaryOf(filtered)}>选中当前匹配的副账号</Button>
              <span style={{ fontSize: 12, color: '#888' }}>预设只选副账号；主账号需手动勾选。</span>
            </Space>
            <Table
              rowKey="kiro_user_id"
              dataSource={filtered}
              columns={flatCols}
              loading={loading}
              size="small"
              pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个账号` }}
              rowSelection={{
                selectedRowKeys: selectedKeys,
                onChange: (keys) => setSelectedKeys(keys as string[]),
                preserveSelectedRowKeys: true,
                getCheckboxProps: (r: AccountRow) => ({ disabled: !hasLiveSubscription(r) }),
              }}
            />
            {/* 批量操作条 */}
            {selectedKeys.length > 0 && (
              <Alert type={selectedPrimary > 0 ? 'error' : 'warning'} showIcon
                message={
                  <Space size={12}>
                    <span>已选 <b>{selectedKeys.length}</b> 个账号</span>
                    {selectedPrimary > 0 && <span style={{ color: '#cf1322' }}>其中 <b>{selectedPrimary}</b> 个主账号</span>}
                    <Button size="small" type="primary" danger onClick={openBatchFromSelection}>批量停用 / 回收</Button>
                    <Button size="small" onClick={() => setSelectedKeys([])}>清空选择</Button>
                  </Space>
                } />
            )}
          </Space>
        )}
      </Card>
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
        分组即 IAM Identity Center 的 Group（申请表单的分组下拉从这里拉取）。
        新建后用户申请账号时即可选择；开通时账号会加入该组。
      </Typography.Paragraph>
      <Space style={{ marginBottom: 16 }}>
        <Input
          placeholder="新分组名，如 Team-Backend"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onPressEnter={doCreate}
          style={{ width: 280 }}
        />
        <Button type="primary" loading={creating} disabled={!newName.trim()} onClick={doCreate}>
          新建分组
        </Button>
      </Space>
      <Table
        rowKey={(g) => g}
        dataSource={groups}
        columns={[{ title: '分组名', render: (g: string) => g }]}
        loading={loading}
        size="small"
        pagination={{ pageSize: 15 }}
      />
    </Card>
  )
}

function ImportPanel() {
  const [rows, setRows] = useState<UnlinkedAccount[]>([])
  const [users, setUsers] = useState<FeishuUser[]>([])
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  // 每行手工选择的绑定目标 open_id（覆盖建议）
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
    c === 'email' ? <Tag color="green">邮箱匹配</Tag>
    : c === 'pinyin' ? <Tag color="orange">拼音匹配</Tag>
    : <Tag>无建议</Tag>

  const cols = [
    { title: 'IDC 用户名', dataIndex: 'kiro_username' },
    { title: '显示名', dataIndex: 'display_name' },
    { title: '邮箱', dataIndex: 'kiro_email' },
    { title: '建议归属', render: (_: any, r: UnlinkedAccount) => (
      <Space size={4}>
        {confTag(r.confidence)}
        {r.suggested_name && <span>{r.suggested_name}</span>}
      </Space>
    ) },
    { title: '绑定给', render: (_: any, r: UnlinkedAccount) => (
      <Select
        style={{ width: 180 }}
        placeholder="选择飞书用户"
        showSearch
        optionFilterProp="label"
        value={picks[r.kiro_user_id] || r.suggested_open_id || undefined}
        onChange={(v) => setPicks((p) => ({ ...p, [r.kiro_user_id]: v }))}
        options={users.map((u) => ({ value: u.open_id, label: u.name }))}
      />
    ) },
    { title: '操作', render: (_: any, r: UnlinkedAccount) => {
      const target = picks[r.kiro_user_id] || r.suggested_open_id
      const name = users.find((u) => u.open_id === target)?.name || r.suggested_name
      return (
        <Popconfirm title={`确认把 ${r.kiro_username} 绑定给 ${name || '?'}？`}
          onConfirm={() => doLink(r)} disabled={!target}>
          <Button size="small" type="primary" disabled={!target}
            loading={linking === r.kiro_user_id}>
            绑定
          </Button>
        </Popconfirm>
      )
    } },
  ]

  return (
    <Card title="存量账号导入"
      extra={<Button type="primary" loading={loading} onClick={scan}>
        {loaded ? '重新扫描' : '扫描游离账号'}
      </Button>}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        游离账号 = IDC 里存在但未绑定飞书用户的账号（非本平台开通的存量）。
        绑定后账号会出现在对方的「我的 Kiro 账号」，套餐/状态/用量自动按实况显示。
        建议归属：<Tag color="green">邮箱匹配</Tag>可信度高；<Tag color="orange">拼音匹配</Tag>请人工核对后再绑定。
        绑定目标下拉仅含在平台登录/申请过的飞书用户。
      </Typography.Paragraph>
      {!loaded && !loading && <Alert type="info" showIcon message="点右上角「扫描游离账号」开始（全量扫 IDC，数秒）" />}
      {loaded && (
        <Table rowKey="kiro_user_id" dataSource={rows} columns={cols}
          loading={loading} size="small" pagination={{ pageSize: 15 }}
          locale={{ emptyText: '没有游离账号，全部已关联 ✅' }} />
      )}
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
            { key: 'groups', label: '分组管理', children: <GroupsPanel /> },
            { key: 'import', label: '存量导入', children: <ImportPanel /> },
          ]}
        />
      </Content>
    </Layout>
  )
}