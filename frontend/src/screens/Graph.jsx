import { useMemo } from 'react'
import { useFetch } from './useDataOp'

const W = 720, H = 460, CX = W / 2, CY = H / 2

// 按 entity_type 区分形状/颜色（issue #84 · 全图谱跨类型渲染）
const TYPE_STYLE = {
  person:  { fill: 'var(--s1)', shape: 'circle' },
  company: { fill: 'var(--s2)', shape: 'rect' },
  asset:   { fill: 'var(--s3)', shape: 'diamond' },
  family:  { fill: 'var(--warn)', shape: 'rect' },
}
const TYPE_LABEL = {
  person: '人物图谱',
  company: '公司图谱',
  family: '全图谱（人·公司·资产·家族）',
}

/**
 * 通用只读图谱视图（F-P1-04/05 + /graph/all）：SVG 环形布局 + 按 entity_type 形状/颜色区分。
 * 纯类型视图（/graph/persons、/graph/companies）单色；/graph/all 多类型混合。
 * 静态布局，无交互拖拽（G6/ECharts 留待需要交互时再换）。
 */
export default function Graph({ url, emptyHint, action }) {
  const g = useFetch(url)
  const { nodes = [], edges = [] } = g.data || {}
  const isAll = url.startsWith('/api/v1/graph/all')
  const title = isAll ? TYPE_LABEL.family
    : (url.startsWith('/api/v1/graph/persons') ? TYPE_LABEL.person : TYPE_LABEL.company)

  const layout = useMemo(() => {
    if (!nodes.length) return {}
    const pos = {}
    const R = Math.min(CX, CY) - 90
    nodes.forEach((n, i) => {
      const ang = (i / nodes.length) * Math.PI * 2 - Math.PI / 2
      pos[n.id] = { x: CX + R * Math.cos(ang), y: CY + R * Math.sin(ang) }
    })
    return { pos, R }
  }, [nodes])

  const pos = layout.pos || {}
  return (
    <div className="panel">
      <h3>{title}{action && <span style={{ float: 'right', marginLeft: 8 }}>{action}</span>}</h3>
      <p className="note">只读视图 · {nodes.length} 节点 · {edges.length} 关系（rel_type 标注）{isAll ? ' · 形状/颜色按 entity_type 区分' : ''}</p>
      {nodes.length === 0 ? (
        <div className="plot"><span className="ph">{emptyHint || '暂无节点（需 entity/relationship 数据）'}</span></div>
      ) : (
        <div className="plot" style={{ height: 460 }}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
            {/* 连线 */}
            {edges.map((e, i) => {
              const a = pos[e.from], b = pos[e.to]
              if (!a || !b) return null
              const isCross = isAll && e.from_type && e.to_type && e.from_type !== e.to_type
              return (
                <g key={i}>
                  <line x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke={isCross ? 'var(--warn)' : 'var(--axis)'}
                    strokeWidth={isCross ? '1.6' : '1.2'}
                    strokeDasharray={isCross ? '4 3' : ''} />
                  <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2} fontSize="9" fill="var(--muted)" textAnchor="middle" dy="-2">
                    {e.rel_type}
                  </text>
                </g>
              )
            })}
            {/* 节点（按 entity_type 形状/颜色） */}
            {nodes.map(n => {
              const p = pos[n.id]
              if (!p) return null
              const style = (isAll && TYPE_STYLE[n.type]) || { fill: 'var(--s1)', shape: 'circle' }
              const shapeNode = renderShape(p, n, style)
              return (
                <g key={n.id}>
                  {shapeNode}
                  <text x={p.x} y={p.y} fontSize="9" fill="#fff" textAnchor="middle" dominantBaseline="central">
                    {String(n.id)}
                  </text>
                  <text x={p.x} y={p.y + (style.shape === 'circle' || style.shape === 'diamond' ? 26 : 22)} fontSize="10" fill="var(--ink)" textAnchor="middle">
                    {n.name}
                  </text>
                </g>
              )
            })}
          </svg>
          {isAll && (
            <div className="legend" style={{ position: 'absolute', bottom: 8, right: 12, background: 'var(--surface-1)', padding: '4px 8px', borderRadius: 6, border: '1px solid var(--border)' }}>
              <span><i style={{ background: TYPE_STYLE.person.fill, borderRadius: '50%' }} /> person</span>
              <span><i style={{ background: TYPE_STYLE.company.fill }} /> company</span>
              <span><i style={{ background: TYPE_STYLE.asset.fill, transform: 'rotate(45deg)' }} /> asset</span>
              <span><i style={{ background: TYPE_STYLE.family.fill }} /> family</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function renderShape(p, n, style) {
  const fill = style.fill
  const stroke = 'var(--page)', sw = 2
  if (style.shape === 'rect') {
    return <rect x={p.x - 18} y={p.y - 12} width={36} height={24} rx={3}
      fill={fill} opacity="0.9" stroke={stroke} strokeWidth={sw} />
  }
  if (style.shape === 'diamond') {
    return <polygon points={`${p.x},${p.y - 16} ${p.x + 16},${p.y} ${p.x},${p.y + 16} ${p.x - 16},${p.y}`}
      fill={fill} opacity="0.9" stroke={stroke} strokeWidth={sw} />
  }
  return <circle cx={p.x} cy={p.y} r={16} fill={fill} opacity="0.85" stroke={stroke} strokeWidth={sw} />
}