import { useEffect, useState } from 'react'

export default function Dashboard({ asOf }) {
  const [ov, setOv] = useState(null)
  const [wealth, setWealth] = useState(null)
  const [snaps, setSnaps] = useState(null)

  const year = Number(asOf.split('-')[0])

  useEffect(() => {
    fetch('/api/v1/overview').then(r => r.json()).then(setOv).catch(() => setOv(null))
  }, [])

  useEffect(() => {
    fetch(`/api/v1/snapshots?as_of=${asOf}`).then(r => r.json()).then(setSnaps).catch(() => setSnaps([]))
  }, [asOf])

  useEffect(() => {
    fetch(`/api/v1/wealth?year_from=${Math.max(1947, year - 10)}&year_to=${year}`)
      .then(r => r.json()).then(setWealth).catch(() => setWealth(null))
  }, [year])

  const totals = snaps && snaps.reduce((m, s) => m + s.value, 0)

  return (
    <div className="grid">
      <div className="grid stats">
        <Stat label={`总资产（${asOf} · 展示折USD）`} value={totals !== null && totals !== undefined ? formatNum(totals) : '—'} />
        <Stat label="实体 / 账户" value={ov ? `${ov.entities} / ${ov.accounts}` : '—'} />
        <Stat label="快照 / 收益流" value={ov ? `${ov.snapshots} / ${ov.income_streams}` : '—'} />
      </div>

      <div className="grid cols2">
        <div className="panel">
          <h3>家族财富随时间</h3>
          <p className="note">wealth API · 展示 USD 折算（近 {year >= 1947 ? Math.min(10, year - 1947) + 1 : 1} 年）</p>
          <div className="plot">
            {wealth && (
              <svg viewBox="0 0 600 200" preserveAspectRatio="none">
                <Polyline data={Object.values(wealth).map(w => w.family_total_usd)} />
              </svg>
            )}
            <span className="ph">
              {wealth ? `共 ${Object.keys(wealth).length} 年` : '财富曲线（待数据）'}
            </span>
          </div>
        </div>

        <div className="panel">
          <h3>{asOf} 各账户 as-of 快照</h3>
          <table>
            <thead><tr><th>账户</th><th className="num">value</th><th>币种</th></tr></thead>
            <tbody>
              {snaps && snaps.map(s => (
                <tr key={s.scope}>
                  <td className="mono">{s.scope}</td>
                  <td className="num">{formatNum(s.value)}</td>
                  <td>{s.currency}</td>
                </tr>
              ))}
              {snaps && snaps.length === 0 && <tr><td colSpan={3} className="note">该日期无快照</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div className="hero">
      <div className="label">{label}</div>
      <div className="fig">{value}</div>
    </div>
  )
}

function Polyline({ data }) {
  if (!data || data.length < 2) return null
  const w = 600, h = 200, pad = 20
  const vals = data.map(Number)
  const max = Math.max(...vals), min = Math.min(...vals)
  const range = max - min || 1
  const pts = vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (w - 2 * pad)
    const y = h - pad - ((v - min) / range) * (h - 2 * pad)
    return `${x},${y}`
  }).join(' ')
  return (
    <polyline
      points={pts}
      fill="none"
      stroke="var(--s1)"
      strokeWidth="2.5"
    />
  )
}

function formatNum(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  const abs = Math.abs(n)
  if (abs >= 1e8) return (n / 1e8).toFixed(2) + ' 亿'
  if (abs >= 1e4) return (n / 1e4).toFixed(1) + ' 万'
  return n.toLocaleString()
}