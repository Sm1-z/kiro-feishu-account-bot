// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** 轻量纯 SVG 图表，避免引入 echarts/recharts 增大 bundle。 */

const PALETTE = ['#4361ee', '#3a0ca3', '#7209b7', '#f72585', '#4cc9f0',
                 '#4895ef', '#f77f00', '#06d6a0']

interface BarItem { label: string; value: number }

/** 横向柱状图（Top 用户 credits / 消息）。 */
export function HBarChart({ data, unit = '', height = 240 }: {
  data: BarItem[]; unit?: string; height?: number
}) {
  if (!data.length) return <Empty />
  const max = Math.max(...data.map((d) => d.value), 1)
  const rowH = Math.min(34, (height - 10) / data.length)
  return (
    <svg width="100%" height={height} style={{ overflow: 'visible' }}>
      {data.map((d, i) => {
        const w = (d.value / max) * 62 // 百分比宽度，留右侧给数值
        const y = i * rowH + 4
        return (
          <g key={i}>
            <text x={0} y={y + rowH / 2} dominantBaseline="middle"
              fontSize={12} fill="#555">{truncate(d.label, 14)}</text>
            <rect x="32%" y={y + 3} width={`${w}%`} height={rowH - 8}
              rx={3} fill={PALETTE[i % PALETTE.length]} />
            <text x={`${34 + w}%`} y={y + rowH / 2} dominantBaseline="middle"
              fontSize={11} fill="#333">{fmt(d.value)}{unit}</text>
          </g>
        )
      })}
    </svg>
  )
}

/** 环形饼图（tier 分布 / 主副占比）。 */
export function DonutChart({ data, size = 200 }: {
  data: BarItem[]; size?: number
}) {
  const total = data.reduce((s, d) => s + d.value, 0)
  if (!total) return <Empty />
  const r = size / 2 - 4
  const cx = size / 2
  const cy = size / 2
  let acc = 0
  const arcs = data.map((d, i) => {
    const frac = d.value / total
    const a0 = acc * 2 * Math.PI - Math.PI / 2
    acc += frac
    const a1 = acc * 2 * Math.PI - Math.PI / 2
    const large = frac > 0.5 ? 1 : 0
    const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0)
    const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1)
    return { path: `M${cx},${cy} L${x0},${y0} A${r},${r} 0 ${large},1 ${x1},${y1} Z`,
             color: PALETTE[i % PALETTE.length], label: d.label, value: d.value, frac }
  })
  return (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
      <svg width={size} height={size}>
        {arcs.map((a, i) => <path key={i} d={a.path} fill={a.color} />)}
        <circle cx={cx} cy={cy} r={r * 0.55} fill="#fff" />
        <text x={cx} y={cy} textAnchor="middle" dominantBaseline="middle"
          fontSize={20} fontWeight={700} fill="#333">{fmt(total)}</text>
      </svg>
      <div>
        {arcs.map((a, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4, fontSize: 12 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: a.color, display: 'inline-block' }} />
            <span>{a.label}</span>
            <span style={{ color: '#888' }}>{a.value}（{(a.frac * 100).toFixed(0)}%）</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function Empty() {
  return <div style={{ color: '#ccc', fontSize: 13, padding: 20, textAlign: 'center' }}>暂无数据</div>
}
function truncate(s: string, n: number) { return s.length > n ? s.slice(0, n) + '…' : s }
function fmt(v: number) {
  return v >= 1000 ? (v / 1000).toFixed(1) + 'k' : (Number.isInteger(v) ? v : v.toFixed(1))
}