import { useEffect, useMemo, useState } from 'react'

/** P0-2/#124 序列选择：family=家族合计(USD)；BEF…=分币种合计；account:* =单账户。 */
export default function Dashboard({ asOf }) {
  const [ov, setOv] = useState(null)
  const [wealth, setWealth] = useState(null)
  const [snaps, setSnaps] = useState(null)
  const [series, setSeries] = useState('family')

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

  // issue #111：总资产只取 family:total 行（USD 口径）。
  // 快照接口返回 account(本币)/entity(分币)/family 三层混合行，直接求和会混币种+三层重复计数。
  const familyTotal = snaps ? snaps.find(s => s.scope === 'family:total') : null

  const scopeLayer = (scope) => {
    if (!scope) return ''
    if (scope.startsWith('account:')) return '账户·本币'
    if (scope.startsWith('entity:')) return '实体·分币'
    if (scope.startsWith('family:')) return '家族·USD'
    return '其他'
  }

  // issue #124：多序列对比——后端已返回 accounts/currencies breakdown，此前被弃用
  const years = wealth ? Object.keys(wealth).map(Number).sort((a, b) => a - b) : []
  const currencyKeys = useMemo(() => {
    const set = new Set()
    for (const w of Object.values(wealth || {})) {
      for (const c of Object.keys(w.currencies || {})) set.add(c)
    }
    return [...set].sort()
  }, [wealth])
  const accountKeys = useMemo(() => {
    const set = new Set()
    for (const w of Object.values(wealth || {})) {
      for (const a of Object.keys(w.accounts || {})) set.add(a)
    }
    return [...set].sort()
  }, [wealth])

  const seriesData = useMemo(() => {
    if (!wealth) return null
    if (series === 'family') return years.map(y => wealth[y]?.family_total_usd ?? 0)
    if (currencyKeys.includes(series)) return years.map(y => wealth[y]?.currencies?.[series] ?? 0)
    return years.map(y => wealth[y]?.accounts?.[series] ?? 0)
  }, [wealth, series, years, currencyKeys])

  const missingRates = useMemo(() => {
    const s = new Set()
    for (const w of Object.values(wealth || {})) for (const m of (w.missing_rates || [])) s.add(m[0])
    return [...s]
  }, [wealth])

  const seriesLabel = series === 'family' ? '家族合计（USD）'
    : currencyKeys.includes(series) ? `币种 ${series}（本币合计）`
      : `账户 ${series}（本币）`

  return (
    <div className="grid">
      <div className="grid stats">
        <Stat label={`总资产（${asOf} · 展示折USD）`} value={familyTotal ? formatNum(familyTotal.value) : '—'} />
        <Stat label="实体 / 账户" value={ov ? `${ov.entities} / ${ov.accounts}` : '—'} />
        <Stat label="快照 / 收益流" value={ov ? `${ov.snapshots} / ${ov.income_streams}` : '—'} />
      </div>

      <div className="grid cols2">
        <div className="panel">
          <h3>家族财富随时间（多序列）</h3>
          <p className="note">wealth API · 近 {year >= 1947 ? Math.min(10, year - 1947) + 1 : 1} 年 · issue #124 单/多序列对比</p>
          <div className="row" style={{ alignItems: 'center', marginBottom: 6 }}>
            <select value={series} onChange={e => setSeries(e.target.value)}>
              <option value="family">家族合计（展示折USD）</option>
              {currencyKeys.length > 0 && (
                <optgroup label="分币种合计（本币）">
                  {currencyKeys.map(c => <option key={c} value={c}>{c}</option>)}
                </optgroup>
              )}
              {accountKeys.length > 0 && (
                <optgroup label="单账户（本币）">
                  {accountKeys.map(a => <option key={a} value={a}>{a}</option>)}
                </optgroup>
              )}
            </select>
            <span className="note">{seriesLabel}</span>
          </div>
          {missingRates.length > 0 && (
            <div className="warn">汇率缺失未计入 USD 合计：{missingRates.join('、')}</div>
          )}
          <div className="plot">
            {seriesData && seriesData.length > 1 && (
              <svg viewBox="0 0 600 200" preserveAspectRatio="none">
                <Polyline data={seriesData} />
              </svg>
            )}
            <span className="ph">
              {seriesData && seriesData.length > 1 ? `共 ${seriesData.length} 年` : '财富曲线（数据不足）'}
            </span>
          </div>
        </div>

        <div className="panel">
          <h3>{asOf} 各账户 as-of 快照</h3>
          <table>
            <thead><tr><th>账户</th><th>层级</th><th className="num">value</th><th>币种</th></tr></thead>
            <tbody>
              {snaps && snaps.map(s => (
                <tr key={s.scope}>
                  <td className="mono">{s.scope}</td>
                  <td>{scopeLayer(s.scope)}</td>
                  <td className="num">{formatNum(s.value)}</td>
                  <td>{s.currency}</td>
                </tr>
              ))}
              {snaps && snaps.length === 0 && <tr><td colSpan={4} className="note">该日期无快照</td></tr>}
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