// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Table, Tag, Modal, Form, Input, InputNumber, Select, message, Space,
  Typography, Layout, Empty, Tooltip,
} from 'antd'
import {
  getMe, applyAccount, requestLimitChange, myRequests, getGroups, isAccountActive,
  LIMIT_SCOPE_LABEL, Me, Account, ReqItem,
} from '../api'

const { Header, Content } = Layout
const { Title } = Typography

const poolTag = (a: Account) => {
  if (!a.live_synced)
    return <Tag color={a.status === 'active' ? 'green' : 'default'}>{a.status === 'active' ? '已开通' : '已停用'}（快照）</Tag>
  if (a.pool_status === 'SEATED') return <Tag color="green">已开通</Tag>
  if (a.pool_status === 'AWAITING_SEAT') return <Tag color="orange" title="管理员买到席位后自动可用">等待席位</Tag>
  return <Tag color="red">已停用</Tag>
}

const limitText = (a: Account) => {
  if (a.limit_scope == null) return <span style={{ color: '#ccc' }}>—</span>
  if (a.limit_scope === 'NONE') return <span>不限 <Tag style={{ marginLeft: 4 }}>无上限</Tag></span>
  return <span>{a.limit?.toLocaleString()} <Tag style={{ marginLeft: 4 }}>{LIMIT_SCOPE_LABEL[a.limit_scope]}</Tag></span>
}

const usedText = (a: Account) => {
  if (a.month_credits == null) return <span style={{ color: '#ccc' }}>—</span>
  const pct = a.limit ? a.month_credits / a.limit : 0
  const color = pct >= 1 ? '#cf1322' : pct >= 0.8 ? '#d46b08' : undefined
  return <span style={{ color }}>{a.month_credits.toLocaleString(undefined, { maximumFractionDigits: 1 })}
    {a.limit ? ` / ${a.limit.toLocaleString()}` : ''}</span>
}

const REQ_LABEL: Record<string, string> = { apply: '开通', limit_change: '调整上限' }
const reqDetail = (r: ReqItem) => {
  const p = r.payload || {}
  if (r.type === 'apply')
    return `${p.username} · ${p.group || '默认组'} · 上限 ${p.usage_limit ? `${p.usage_limit} credits/月` : '默认'}`
  if (r.type === 'limit_change')
    return `${p.username}：${p.current_limit ?? '默认'} → ${p.requested_limit} credits/月`
  return JSON.stringify(p)
}

export default function Dashboard() {
  const nav = useNavigate()
  const [me, setMe] = useState<Me | null>(null)
  const [reqs, setReqs] = useState<ReqItem[]>([])
  const [applyOpen, setApplyOpen] = useState(false)
  const [limitTarget, setLimitTarget] = useState<Account | null>(null)
  const [groups, setGroups] = useState<string[]>([])
  const [form] = Form.useForm()
  const [limitForm] = Form.useForm()

  const [refreshing, setRefreshing] = useState(false)
  const load = async () => {
    setRefreshing(true)
    try {
      const m = await getMe()
      setMe(m)
      setReqs(await myRequests())
    } catch { message.error('加载失败') } finally { setRefreshing(false) }
  }
  useEffect(() => { load() }, [])
  // 分组从 IDC 动态拉取（失败时后端已降级为默认组）
  useEffect(() => { getGroups().then(setGroups).catch(() => setGroups([])) }, [])

  // 每人一个账号：已有账号（含已停用）就不能再申请；停用的找管理员恢复
  const hasAccount = !!me && me.accounts.length > 0
  const pendingApply = reqs.some((r) => r.type === 'apply' && (r.status === 'pending' || r.status === 'approved'))

  const submitApply = async () => {
    const v = await form.validateFields()
    try {
      await applyAccount({ ...v, usage_limit: v.usage_limit ?? null })
      message.success('申请已提交，等待管理员审批')
      setApplyOpen(false); form.resetFields(); load()
    } catch (e: any) { message.error(e.response?.data?.detail || '提交失败') }
  }

  const submitLimit = async () => {
    const v = await limitForm.validateFields()
    try {
      await requestLimitChange({ kiro_user_id: limitTarget!.kiro_user_id, requested_limit: v.requested_limit, reason: v.reason })
      message.success('调整上限申请已提交')
      setLimitTarget(null); limitForm.resetFields(); load()
    } catch (e: any) { message.error(e.response?.data?.detail || '提交失败') }
  }

  // 状态 / 上限展示积分池实况（控制台直接移出 / 改上限不回写映射表，快照会滞后）
  const acctCols = [
    { title: '用户名', dataIndex: 'kiro_username' },
    { title: '邮箱', dataIndex: 'kiro_email' },
    { title: '分组', dataIndex: 'team' },
    { title: '状态', render: (_: any, r: Account) => poolTag(r) },
    { title: '本月上限（credits）', render: (_: any, r: Account) => limitText(r) },
    { title: <span title="来自 Kiro 用量报表，约有 1 天延迟">本月已用</span>, render: (_: any, r: Account) => usedText(r) },
    { title: '操作', render: (_: any, r: Account) =>
      <Button size="small" disabled={!isAccountActive(r)}
        onClick={() => {
          setLimitTarget(r)
          limitForm.setFieldsValue({ requested_limit: r.limit ? Math.ceil(r.limit * 2) : undefined, reason: '' })
        }}>
        申请调整上限
      </Button> },
  ]

  const reqCols = [
    { title: '类型', dataIndex: 'type', render: (t: string) => REQ_LABEL[t] || t },
    { title: '详情', render: (_: any, r: ReqItem) => <span style={{ fontSize: 12 }}>{reqDetail(r)}</span> },
    { title: '状态', dataIndex: 'status', render: (s: string) => {
      const c = { pending: 'orange', approved: 'blue', executed: 'green', failed: 'red', rejected: 'default' } as any
      return <Tag color={c[s]}>{s}</Tag>
    } },
    { title: '说明', render: (_: any, r: ReqItem) => {
      // failed 的原因在 result.error；rejected 在 review_comment；成功但有待办（如等待席位）在 result.warning
      const reason = r.result?.error || r.review_comment
      if (reason) return <Typography.Text type="danger" style={{ fontSize: 12 }}>{reason}</Typography.Text>
      if (r.result?.warning) return <Typography.Text type="warning" style={{ fontSize: 12 }}>{r.result.warning}</Typography.Text>
      return <span style={{ color: '#ccc' }}>—</span>
    } },
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Title level={4} style={{ margin: 0 }}>Kiro 账号管理</Title>
        <Space>
          <span>{me?.name}</span>
          {me?.is_admin && <Button onClick={() => nav('/admin')}>管理后台</Button>}
          <Button onClick={() => { localStorage.removeItem('token'); nav('/login') }}>退出</Button>
        </Space>
      </Header>
      <Content style={{ padding: 24 }}>
        <Card
          title="我的 Kiro 账号"
          extra={
            <Space>
              <Button loading={refreshing} onClick={load}>刷新</Button>
              <Tooltip title={hasAccount ? '每人一个 Kiro 账号；账号被停用请联系管理员恢复'
                : pendingApply ? '你的开通申请正在审批中' : ''}>
                <Button type="primary" disabled={hasAccount || pendingApply} onClick={() => {
                  form.setFieldsValue({ username: me?.suggested_username })
                  setApplyOpen(true)
                }}>
                  申请账号
                </Button>
              </Tooltip>
            </Space>
          }
          style={{ marginBottom: 24 }}
        >
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
            公司的 Kiro credits 由所有人共享（积分池），每人一个账号。每个账号每月最多能用到「本月上限」；用完后当月暂停，下月 1 日（UTC）重置，或申请调整上限。
          </Typography.Paragraph>
          <Table rowKey="kiro_user_id" dataSource={me?.accounts || []} columns={acctCols}
            pagination={false} locale={{ emptyText: <Empty description="还没有账号，点右上角申请" /> }} />
        </Card>

        <Card title="我的申请记录">
          <Table rowKey="request_id" dataSource={reqs} columns={reqCols} pagination={{ pageSize: 5 }} />
        </Card>
      </Content>

      <Modal title="申请新 Kiro 账号" open={applyOpen} onOk={submitApply}
        onCancel={() => setApplyOpen(false)} okText="提交申请">
        <Form form={form} layout="vertical">
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input placeholder="系统推荐下一个可用用户名" />
          </Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: 'email' }]}>
            <Input placeholder="用于接收密码设置邮件" />
          </Form.Item>
          <Space>
            <Form.Item name="given_name" label="Given Name"><Input placeholder="名" /></Form.Item>
            <Form.Item name="family_name" label="Family Name"><Input placeholder="姓" /></Form.Item>
          </Space>
          <Form.Item name="group" label="分组" rules={[{ required: true }]}>
            <Select
              options={groups.map((g) => ({ value: g, label: g }))}
              placeholder="选择团队分组"
              notFoundContent="加载分组中…"
            />
          </Form.Item>
          <Form.Item name="usage_limit" label="每月 credits 上限（可选）"
            extra="不填 = 使用默认上限（组上限或公司默认）。需要更多额度时再填，并写明理由。">
            <InputNumber min={1} max={1000000} precision={0} style={{ width: '100%' }} placeholder="例如 2000" addonAfter="credits / 月" />
          </Form.Item>
          <Form.Item name="reason" label="申请理由">
            <Input.TextArea rows={2} maxLength={500} placeholder="例如：负责 XX 项目的代码迁移" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title={`申请调整上限 · ${limitTarget?.kiro_username}`} open={!!limitTarget}
        onOk={submitLimit} onCancel={() => setLimitTarget(null)} okText="提交申请">
        <Form form={limitForm} layout="vertical">
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            当前上限：{limitTarget ? limitText(limitTarget) : '—'}；本月已用：{limitTarget ? usedText(limitTarget) : '—'}
          </Typography.Paragraph>
          <Form.Item name="requested_limit" label="申请的新上限" rules={[{ required: true }]}>
            <InputNumber min={1} max={1000000} precision={0} style={{ width: '100%' }} addonAfter="credits / 月" />
          </Form.Item>
          <Form.Item name="reason" label="理由">
            <Input.TextArea rows={2} maxLength={500} placeholder="例如：本月有大型重构，预计用量翻倍" />
          </Form.Item>
        </Form>
      </Modal>
    </Layout>
  )
}
