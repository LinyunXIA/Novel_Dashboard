import { useMemo, useState } from 'react'
import { useFetch } from './useDataOp'
import { Field } from './ui'

const RISK_COLOR = { R1: 'var(--s3)', R2: '#2a78d6', R3: '#8878e0', R4: 'var(--s2)', R5: 'var(--crit)' }
const RISK_ORDER = ['R1', 'R2', 'R3', 'R4', 'R5']

/** F-P1-06 各国收益曲线：R1–R5 五条 SVG 线对比。
 *  issue #87-3：国家下拉来自 /returns/countries，不再前端硬编码 9 国。 */
export default function Returns({ asOf }) {
  const [country, setCountry] = useState('')
  const countries = useFetch('/api/v1/returns/countries')
  const countryList = countries.data?.countries || []
  // 国家列表加载后，取当前选中在库者；否则回退首个在库国家（避免硬编码默认）
  const effective = (country && countryList.includes(country)) ? country : (countryList[0] || '比利时')
  const data = useFetch(effective ? `/api/v1/returns?country=${encodeURIComponent(effective)}&page_size=500` : null)
  const regions = useFetch('/api/v1/returns/regions')
  // issue #121：收益曲线窗口截至全局日历游标所在年
  const asOfYear = asOf ? Number(asOf.split('-')[0]) : null

  const series = useMemo(() => {
    const byR = {}
    for (const r of (data.data?.items || [])) {
      if (asOfYear && r.year > asOfYear) continue
      ;(byR[r.risk_lvl] ||= []).push([r.year, Number(r.rate)])
    }
    for (const k of Object.keys(byR)) byR[k].sort((a, b) => a[0] - b[0])
    return byR
  }, [data.data, asOfYear])

  const ys = (data.data?.items || [])
    .filter(r => !asOfYear || r.year <= asOfYear)
    .flatMap(r => r.rate == null ? [] : [Number(r.rate)])

  return (
    <div className="cols2">
      <div className="panel">
        <h3>各国收益曲线（R1–R5 对比）</h3>
        <p className="note">DESIGN §14 returns · §19.3 地区起始年下限由 /returns/regions 标注</p>
        <Field label="国家（来自 /returns/countries）">
          <select value={effective} onChange={e => setCountry(e.target.value)}>
            {countryList.length ? countryList.map(c => <option key={c}>{c}</option>)
              : <option>{effective}</option>}
          </select>
        </Field>
        <div className="plot" style={{ height: 300 }}>
          {Object.keys(series).length ? (
            <svg viewBox="0 0 620 280" preserveAspectRatio="none">
              {RISK_ORDER.filter(r => series[r]).map(r => (
                <RiskLine key={r} pts={series[r]} color={RISK_COLOR[r]}
                  minY={Math.min(...ys, 0)} maxY={Math.max(...ys, 1)} />
              ))}
            </svg>
          ) : <span className="ph">无收益数据（该国家 R1–R5 需 return_curve 导入）</span>}
        </div>
        <div className="legend">
          {RISK_ORDER.map(r => <span key={r}><i style={{ background: RISK_COLOR[r] }} />{r}</span>)}
        </div>
        <p className="note">地区起始年下限：{Object.entries(regions.data || {}).map(([r, m]) => `${r} ${m.start_year}`).join(' · ')}</p>
      </div>
    </div>
  )
}

function RiskLine({ pts, color, minY, maxY }) {
  const W = 620, H = 260, pad = 24
  if (!pts.length) return null
  const years = pts.map(p => p[0])
  const x0 = Math.min(...years), x1 = Math.max(...years)
  const range = (maxY - minY) || 1
  const seg = (x1 - x0) || 1
  const d = pts.map(([y, v], i) => {
    const X = pad + ((y - x0) / seg) * (W - 2 * pad)
    const Y = H - pad - ((v - minY) / range) * (H - 2 * pad)
    return `${i === 0 ? 'M' : 'L'}${X},${Y}`
  }).join(' ')
  return <path d={d} fill="none" stroke={color} strokeWidth="2" />
}