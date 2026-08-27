import { useMemo, useState } from 'react'
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
 * 图谱视图（F-P1-04/05 + /graph/all + #197）。
 * - 边：虚线=亲缘推理建议（inferred），实线=人工/文件显式。
 * - 点节点 → 左上资产面板（账户/初始资产/持仓/收益流）。
 * - 关系可编辑：✚ 连线(两节点建实线)、点边删/改/隐藏推理(不复活)。
 */
export default function Graph({ url, emptyHint, action }) {
  const g = useFetch(url)
  const [selected, setSelected] = useState(null)      // 点人看资产
  const [linkFrom, setLinkFrom] = useState(null)       // 连线起点
  const [edgeSel, setEdgeSel] = useState(null)         // 选中的边
  const [busy, setBusy] = useState(false)
  const { nodes = [], edges = [] } = g.data || {}
  const assets = useFetch(selected ? `/api/v1/entities/${selected}/assets` : null)
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
  const nodeName = id => (nodes.find(n => n.id === id) || {}).name || `#${id}`

  async function api(method, path, body) {
    setBusy(true)
    try {
      await fetch(path, { method,
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body) })
    } finally { setBusy(false); g.refresh() }
  }
  const onNode = id => {
    setEdgeSel(null)
    if (linkFrom !== null && linkFrom !== id) {
      const rel = window.prompt(`「${nodeName(linkFrom)}」→「${nodeName(id)}」关系称谓：`)
      if (rel) api('POST', '/api/v1/graph/relationships', { from_id: linkFrom, to_id: id, rel_type: rel })
      setLinkFrom(null)
      return
    }
    setLinkFrom(null)
    setSelected(selected === id ? null : id)
  }
  const onEdge = e => {
    setLinkFrom(null); setSelected(null)
    setEdgeSel(edgeSel === e ? null : e)
  }

  const edgeTool = edgeSel && (
    <div className="note" style={{ border: '1px dashed var(--warn)', padding: '4px 8px', borderRadius: 6 }}>
      <b>边：{nodeName(edgeSel.from)} → {nodeName(edgeSel.to)}（{edgeSel.rel_type}）
        {edgeSel.inferred ? ' · 【推理建议】' : ''}</b>
      {edgeSel.inferred ? (
        <>
          <button disabled={busy} onClick={() => {
            api('POST', '/api/v1/graph/suppress', { from_id: edgeSel.from, to_id: edgeSel.to, rel_type: edgeSel.rel_type })
            setEdgeSel(null)
          }}>删（隐藏建议）</button>
          <button disabled={busy} onClick={() => {
            const rel = window.prompt('改为实线称谓：', edgeSel.rel_type)
            if (rel) { api('POST', '/api/v1/graph/relationships', { from_id: edgeSel.from, to_id: edgeSel.to, rel_type: rel }); setEdgeSel(null) }
          }}>改为实线</button>
        </>
      ) : (
        <>
          <button disabled={busy} onClick={() => {
            if (window.confirm('删除该关系？')) { api('DELETE', `/api/v1/graph/relationships/${edgeSel.id}`); setEdgeSel(null) }
          }}>删除</button>
          <button disabled={busy} onClick={() => {
            const rel = window.prompt('改称谓：', edgeSel.rel_type)
            if (rel) {
              api('DELETE', `/api/v1/graph/relationships/${edgeSel.id}`)
              api('POST', '/api/v1/graph/relationships', { from_id: edgeSel.from, to_id: edgeSel.to, rel_type: rel })
              setEdgeSel(null)
            }
          }}>改称谓</button>
        </>
      )}
      <button onClick={() => setEdgeSel(null)}>取消</button>
    </div>
  )

  return (
    <div className="panel">
      <h3>{title}{action && <span style={{ float: 'right', marginLeft: 8 }}>{action}</span>}</h3>
      <p className="note">
        {nodes.length} 节点 · {edges.length} 关系
        {isAll ? ' · 形状/颜色按 entity_type' : ''} · 虚线=推理建议/实线=人工 · 点节点看资产
        <span style={{ float: 'right' }}>
          <button disabled={busy} onClick={() => {
            if (linkFrom !== null) { setLinkFrom(null); return }
            setEdgeSel(null); setSelected(null); setLinkFrom('MODE')
          }}>{linkFrom !== null ? '✕ 取消连线' : '✚ 连线'}</button>
        </span>
      </p>
      {linkFrom !== null && (
        <p className="note" style={{ color: 'var(--warn)' }}>
          连线模式：先点第一个节点（起点）→ 再点第二个节点 → 输入称谓建实线关系
        </p>
      )}
      {edgeTool}
      {nodes.length === 0 ? (
        <div className="plot"><span className="ph">{emptyHint || '暂无节点（需 entity / 关系数据）'}</span></div>
      ) : (
        <div className="plot" style={{ height: 460 }}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet"
            onClick={() => { if (linkFrom === null) { setSelected(null); setEdgeSel(null) } }}>
            {edges.map((e, i) => {
              const a = pos[e.from], b = pos[e.to]
              if (!a || !b) return null
              const isCross = isAll && e.from_type && e.to_type && e.from_type !== e.to_type
              const dashed = !!e.inferred
              const active = edgeSel === e
              return (
                <g key={`${e.from}-${e.to}-${i}`}>
                  <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="transparent" strokeWidth={14}
                    onClick={ev => { ev.stopPropagation(); onEdge(e) }} />
                  <line x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke={active ? 'var(--warn)' : (isCross ? 'var(--warn)' : 'var(--axis)')}
                    strokeWidth={active ? 2.2 : (isCross ? 1.6 : 1.2)}
                    strokeDasharray={dashed ? '5 4' : (isCross ? '4 3' : '')}
                    pointerEvents="none" />
                  <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2} fontSize="9" fill="var(--muted)"
                    textAnchor="middle" dy="-2" pointerEvents="none"
                    stroke="var(--surface-1)" strokeWidth={2} paintOrder="stroke">
                    {e.rel_type}{dashed ? ' ⁂' : ''}
                  </text>
                </g>
              )
            })}
            {nodes.map(n => {
              const p = pos[n.id]
              if (!p) return null
              const style = (isAll && TYPE_STYLE[n.type]) || { fill: 'var(--s1)', shape: 'circle' }
              return (
                <g key={n.id} style={{ cursor: 'pointer' }} onClick={ev => { ev.stopPropagation(); onNode(n.id) }}>
                  {renderShape(p, n, style)}
                  <text x={p.x} y={p.y} fontSize="9" fill={selected === n.id ? '#000' : '#fff'}
                    textAnchor="middle" dominantBaseline="central" pointerEvents="none"
                    fontWeight={selected === n.id ? 700 : 400}>{String(n.id)}</text>
                  <text x={p.x} y={p.y + (style.shape === 'circle' || style.shape === 'diamond' ? 26 : 22)}
                    fontSize="10" fill="var(--ink)" textAnchor="middle" pointerEvents="none">{n.name}</text>
                </g>
              )
            })}
          </svg>
          {selected && (
            <div className="asset-panel" style={{
              position: 'absolute', left: 8, top: 8, maxWidth: 300, minWidth: 220,
              background: 'var(--surface-1)', border: '1px solid var(--border)', borderRadius: 6,
              padding: 8, maxHeight: 400, overflow: 'auto', fontSize: 12,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <b>{assets.data?.name || nodeName(selected)}</b>
                <button onClick={() => setSelected(null)}>✕</button>
              </div>
              {assetSections(assets.data)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function assetSections(d) {
  if (!d) return <div style={{ color: 'var(--muted)' }}>加载中…</div>
  const sec = (label, rows, fmt) => (
    rows && rows.length ? (
      <div style={{ marginTop: 6 }}>
        <b>{label}（{rows.length}）</b>
        <div>{rows.map((r, k) => <div key={k} style={{ fontSize: 11, color: 'var(--muted)' }}>{fmt(r)}</div>)}</div>
      </div>
    ) : null
  )
  return (
    <>
      {sec('银行账户', d.accounts, a =>
        `${a.currency}${a.bank ? ' · ' + a.bank : ''}${a.status !== 'active' ? ' [' + a.status + ']' : ''}` +
        (a.balance != null ? ` — ${Number(a.balance).toLocaleString()} ${a.currency}` : ''))}
      {sec('初始资产', d.initial_assets, a =>
        `${a.asset_type}${a.name ? ' · ' + a.name : ''}${a.currency ? ' · ' + a.currency : ''}` +
        (a.face_value != null ? ` ${a.face_value.toLocaleString()}` : '') +
        (a.pct != null ? `（${a.pct}%）` : ''))}
      {sec('股票持仓', d.holdings, h =>
        `[${h.event_type}] ${h.company}${h.ticker ? ' (' + h.ticker + ')' : ''} ${h.date}` +
        (h.shares != null ? ` ${Number(h.shares).toLocaleString()} 股` : '') +
        (h.amount_wusd != null ? `  ${h.amount_wusd.toLocaleString()} 万USD` : '') +
        (h.pct != null ? ` ${h.pct}%` : ''))}
      {sec('收益流', d.income, i =>
        `${i.year} ${i.stream_type}${i.group_key ? ' · ' + i.group_key : ''} · ${i.currency}` +
        ` ${Number(i.amount).toLocaleString()}`)}
      {!d.accounts?.length && !d.initial_assets?.length && !d.holdings?.length && !d.income?.length
        ? <div style={{ color: 'var(--muted)', marginTop: 6 }}>暂无资产数据</div> : null}
    </>
  )
}

function renderShape(p, n, style) {
  const fill = style.fill
  const stroke = 'var(--page)', sw = 2
  if (style.shape === 'rect') {
    return <rect x={p.x - 18} y={p.y - 12} width={36} height={24} rx={3} fill={fill} opacity="0.9" stroke={stroke} strokeWidth={sw} />
  }
  if (style.shape === 'diamond') {
    return <polygon points={`${p.x},${p.y - 16} ${p.x + 16},${p.y} ${p.x},${p.y + 16} ${p.x - 16},${p.y}`} fill={fill} opacity="0.9" stroke={stroke} strokeWidth={sw} />
  }
  return <circle cx={p.x} cy={p.y} r={16} fill={fill} opacity="0.85" stroke={stroke} strokeWidth={sw} />
}