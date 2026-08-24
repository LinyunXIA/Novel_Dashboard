import { useMemo } from 'react'
import { useFetch } from './useDataOp'

const W = 720, H = 460, CX = W / 2, CY = H / 2

/**
 * 通用只读图谱视图（F-P1-04/05）：SVG 环形布局（节点等角分布 + 连线 + 名称标签）。
 * 静态布局，无交互拖拽（G6/ECharts 留待需要交互时再换）。
 */
export default function Graph({ url, emptyHint }) {
  const g = useFetch(url)
  const { nodes = [], edges = [] } = g.data || {}

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
      <h3>{url.startsWith('/api/v1/graph/persons') ? '人物图谱' : '公司图谱'}</h3>
      <p className="note">只读视图 · {nodes.length} 节点 · {edges.length} 关系（rel_type 标注）</p>
      {nodes.length === 0 ? (
        <div className="plot"><span className="ph">{emptyHint || '暂无节点（需 entity/relationship 数据）'}</span></div>
      ) : (
        <div className="plot" style={{ height: 460 }}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
            {/* 连线 */}
            {edges.map((e, i) => {
              const a = pos[e.from], b = pos[e.to]
              if (!a || !b) return null
              return (
                <g key={i}>
                  <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="var(--axis)" strokeWidth="1.2" />
                  <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2} fontSize="9" fill="var(--muted)" textAnchor="middle" dy="-2">
                    {e.rel_type}
                  </text>
                </g>
              )
            })}
            {/* 节点 */}
            {nodes.map(n => {
              const p = pos[n.id]
              if (!p) return null
              return (
                <g key={n.id}>
                  <circle cx={p.x} cy={p.y} r="16" fill="var(--s1)" opacity="0.85" stroke="var(--page)" strokeWidth="2" />
                  <text x={p.x} y={p.y} fontSize="9" fill="#fff" textAnchor="middle" dominantBaseline="central">
                    {String(n.id)}
                  </text>
                  <text x={p.x} y={p.y + 26} fontSize="10" fill="var(--ink)" textAnchor="middle">
                    {n.name}
                  </text>
                </g>
              )
            })}
          </svg>
        </div>
      )}
    </div>
  )
}