// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Table, Tag, Modal, Form, Input, Select, message, Space,
  Typography, Layout, Empty,
} from 'antd'
import {
  getMe, applyAccount, upgradeAccount, myRequests, getGroups, isAccountActive,
  Me, Account, ReqItem,
} from '../api'

const { Header, Content } = Layout
const { Title } = Typography

const TIER_OPTS = [
  { value: 'pro', label: 'Kiro Pro ($20)' },
  { value: 'pro+', label: 'Kiro Pro+ ($40)' },
  { value: 'pro max', label: 'Kiro Pro Max ($100)' },
  { value: 'power', label: 'Kiro Power ($200)' },
]
const roleTag = (r: string) =>
  r === 'primary' ? <Tag color="gold">主账号</Tag> : <Tag>副账号</Tag>
const tierTag = (t: string) =>
  t ? <Tag color="blue">{t}</Tag> : <Tag>无</Tag>

export default function Dashboard() {
  const nav = useNavigate()
  const [me, setMe] = useState<Me | null>(null)
  const [reqs, setReqs] = useState<ReqItem[]>([])
  const [applyOpen, setApplyOpen] = useState(false)
  const [upgradeTarget, setUpgradeTarget] = useState<Account | null>(null)
  const [groups, setGroups] = useState<string[]>([])
  const [form] = Form.useForm()
  const [upForm] = Form.useForm()

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

  const activeCount = me ? me.accounts.filter(isAccountActive).length : 0
  const quotaFull = me ? activeCount >= me.quota : false

  const submitApply = async () => {
    const v = await form.validateFields()
    try {
      await applyAccount(v)
      message.success('申请已提交，等待管理员审批')
      setApplyOpen(false); form.resetFields(); load()
    } catch (e: any) { message.error(e.response?.data?.detail || '提交失败') }
  }

  const submitUpgrade = async () => {
    const v = await upForm.validateFields()
    try {
      await upgradeAccount({ kiro_user_id: upgradeTarget!.kiro_user_id, target_tier: v.target_tier })
      message.success('升级申请已提交')
      setUpgradeTarget(null); upForm.resetFields(); load()
    } catch (e: any) { message.error(e.response?.data?.detail || '提交失败') }
  }

  // 套餐/状态展示订阅实况（控制台退订/改套餐不回写映射表，快照会滞后）
  const acctCols = [
    { title: '用户名', dataIndex: 'kiro_username' },
    { title: '邮箱', dataIndex: 'kiro_email' },
    { title: '分组', dataIndex: 'team' },
    { title: '套餐', render: (_: any, r: Account) =>
      tierTag(r.live_synced ? (r.live_tier || '') : r.tier) },
    { title: '类型', dataIndex: 'account_role', render: roleTag },
    { title: '状态', render: (_: any, r: Account) => {
      if (!r.live_synced)
        return <Tag color={r.status === 'active' ? 'green' : 'default'}>{r.status}（快照）</Tag>
      if (!r.live_status) return <Tag color="red">无订阅</Tag>
      const c = { ACTIVE: 'green', PENDING: 'orange' } as any
      return <Tag color={c[r.live_status] || 'default'}>{r.live_status}</Tag>
    } },
    { title: '操作', render: (_: any, r: Account) =>
      <Button size="small" disabled={!isAccountActive(r)}
        onClick={() => {
          setUpgradeTarget(r)
          upForm.setFieldsValue({ target_tier: (r.live_synced ? r.live_tier : r.tier) || r.tier })
        }}>
        升级套餐
      </Button> },
  ]

  const reqCols = [
    { title: '类型', dataIndex: 'type', render: (t: string) =>
      ({ apply: '开通', upgrade: '升级', quota_increase: '配额' } as any)[t] || t },
    { title: '详情', dataIndex: 'payload', render: (p: any) =>
      <span style={{ fontSize: 12 }}>{JSON.stringify(p)}</span> },
    { title: '状态', dataIndex: 'status', render: (s: string) => {
      const c = { pending: 'orange', approved: 'blue', executed: 'green', failed: 'red', rejected: 'default' } as any
      return <Tag color={c[s]}>{s}</Tag>
    } },
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Title level={4} style={{ margin: 0 }}>Kiro 账号管理</Title>
        <Space>
          <span>{me?.name}</span>
          {me?.is_admin && <Button onClick={() => nav('/admin')}>审批后台</Button>}
          <Button onClick={() => { localStorage.removeItem('token'); nav('/login') }}>退出</Button>
        </Space>
      </Header>
      <Content style={{ padding: 24 }}>
        <Card
          title={`我的 Kiro 账号（${activeCount} / ${me?.quota || 0}）`}
          extra={
            <Space>
              <Button loading={refreshing} onClick={load}>刷新</Button>
              <Button type="primary" disabled={quotaFull} onClick={() => {
                form.setFieldsValue({ username: me?.suggested_username })
                setApplyOpen(true)
              }}>
                {quotaFull ? '配额已满' : '申请账号'}
              </Button>
            </Space>
          }
          style={{ marginBottom: 24 }}
        >
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
          <Form.Item name="tier" label="套餐" rules={[{ required: true }]} initialValue="pro">
            <Select options={TIER_OPTS} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title={`升级套餐 · ${upgradeTarget?.kiro_username}`} open={!!upgradeTarget}
        onOk={submitUpgrade} onCancel={() => setUpgradeTarget(null)} okText="提交升级">
        <Form form={upForm} layout="vertical">
          <Form.Item name="target_tier" label="目标套餐" rules={[{ required: true }]}>
            <Select options={TIER_OPTS} />
          </Form.Item>
        </Form>
      </Modal>
    </Layout>
  )
}