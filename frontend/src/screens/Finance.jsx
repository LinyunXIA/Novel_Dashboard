import { useMemo, useState } from 'react'
import { useFetch } from './useDataOp'
import { Field, fmt } from './ui'

const KINDS = ['income', 'expense', 'investment', 'investment_income', 'pool']
const KIND_LABEL = { income: '收入', expense: '支出', investment: '投资', investment_income: '投资损益', pool: '专款池' }

/** F-P1-07 财务收支屏：以实体为中心浏览 finance_entry（实体必填）。 */
export default function Finance({ asOf }) {
  const ents = useFetch('/api/v1/entities?page_size=200')
  const [sel, setSel] = useState('')
  const [kind, setKind] = useState('')
  const [year, setYear] = useState('')
  // issue #121：全局日历游标——展示行截至游标年（输入年份仍可自行过滤更早区间）
  const asOfYear = asOf ? Number(asOf.split('-')[0]) : null

  const url = useMemo(() => {
    if (!sel) return ''
    const q = new URLSearchParams()
    if (kind) q.set('kind', kind)
    if (year) q.set('year', year)
    return `/api/v1/entities/${sel}/finance-entries${q.toString() ? '?' + q.toString() : ''}`
  }, [sel, kind, year])

  const fe = useFetch(url)
  const entOpts = (ents.data?.items || []).filter(e => e.type === 'person' || e.type === 'company')
  const rows = (fe.data?.items || []).filter(r => !asOfYear || (r.year || 0) <= asOfYear)

  const totals = useMemo(() => {
    const t = {}
    for (const r of rows) {
      t['income#' + (r.currency || '')] = (t['income#' + (r.currency || '')] || 0) + (r.kind === 'income' ? (r.amount || 0) : 0)
    }
    return t
  }, [rows])

  return (
    <div className="cols2">
      <div className="panel">
        <h3>财务收支（实体为中心）</h3>
        <p className="note">DESIGN §14 finance-entries · 实体必填 · 来源 file / ui · 截至 {asOf || '—'}</p>
        <div className="row">
          <Field label="实体">
            <select value={sel} onChange={e => setSel(e.target.value)}>
              <option value="">—选择实体—</option>
              {entOpts.map(e => <option key={e.id} value={e.id}>{e.type}:{e.name}</option>)}
            </select>
          </Field>
          <Field label="类别">
            <select value={kind} onChange={e => setKind(e.target.value)}>
              <option value="">全部</option>
              {KINDS.map(k => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
            </select>
          </Field>
          <Field label="年份">
            <input type="number" value={year} placeholder="全部" style={{ width: 80 }}
              onChange={e => setYear(e.target.value)} />
          </Field>
        </div>
        {sel && (
          <table>
            <thead><tr><th>年</th><th>类别</th><th className="num">金额</th><th>币种</th><th>标签</th><th>来源</th></tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id}>
                  <td>{r.year}</td>
                  <td>{KIND_LABEL[r.kind] || r.kind}</td>
                  <td className="num">{r.amount != null ? fmt(r.amount) : '—'}</td>
                  <td>{r.currency}</td>
                  <td className="muted">{r.label}</td>
                  <td>{r.source}</td>
                </tr>
              ))}
              {!rows.length && <tr><td colSpan={6} className="note">无记录</td></tr>}
            </tbody>
          </table>
        )}
      </div>
      {!!sel && Object.keys(totals).length > 0 && (
        <div className="panel">
          <h3>收入汇总</h3>
          {Object.entries(totals).map(([k, v]) => <div key={k} className="row"><span className="label">{k}</span><b className="mono">{fmt(v)}</b></div>)}
        </div>
      )}
    </div>
  )
}