import { useMemo, useState } from 'react'
import { useFetch } from './useDataOp'
import { Field } from './ui'

const RISK_COLOR = { R1: 'var(--s3)', R2: '#2a78d6', R3: '#8878e0', R4: 'var(--s2)', R5: 'var(--crit)' }
const RISK_ORDER = ['R1', 'R2', 'R3', 'R4', 'R5']
const COUNTRIES = ['比利时', '卢森堡', '荷兰', '丹麦', '瑞典', '美国', '英国', '中国香港', '中国大陆']

/** F-P1-06 各国收益曲线：R1–R5 五条 SVG 线对比。 */
export default function Returns() {
  const [country, setCountry] = useState('比利时')
  const data = useFetch(country ? `/api/v1/returns?country=${encodeURIComponent(country)}&page_size=500` : null)
  const regions = useFetch('/api/v1/returns/regions')

  const series = useMemo(() => {
    const byR = {}
    for (const r of (data.data?.items || [])) {
      (byR[r.risk_lvl] ||= []).push([r.year, Number(r.rate)])
    }
    for (const k of Object.keys(byR)) byR[k].sort((a, b) => a[0] - b[0])
    return byR
  }, [data.data])

  const ys = (data.data?.items || []).flatMap(r => r.rate == null ? [] : [Number(r.rate)])

  return (
    <div className="cols2">
      <div className="panel">
        <h3>各国收益曲线（R1–R5 对比）</h3>
        <p className="note">DESIGN §14 returns · §19.3 地区起始年下限由 /returns/regions 标注</p>
        <Field label="国家">
          <select value={country} onChange={e => setCountry(e.target.value)}>
            {COUNTRIES.map(c => <option key={c}>{c}</option>)}
          </select>
        </Field>
        <div className="plot" style={{ height: 300 }}>
          {Object.keys(series).length ? (
            <svg viewBox="0 0 620 280" preserveAspectRatio="none">
              {RISK_ORDER.filter(r => series[r]).map(r => (
                <RiskLine key={r} name={r} pts={series[r]} color={RISK_COLOR[r]} ys={ys}
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

function RiskLine({ name, pts, color, ys, minY, maxY }) {
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