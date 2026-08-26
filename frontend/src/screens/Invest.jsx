import { useState } from 'react'
import { useDataOp, useFetch } from './useDataOp'
import { ErrorBox, Field } from './ui'

const RISK = ['R1', 'R2', 'R3', 'R4', 'R5']

/** F-P1-01/02/09 投资屏：一年一投、R 级计息、年末赎回、失败整体拒绝（useDataOp）。 */
export default function Invest() {
  const regions = useFetch('/api/v1/returns/regions')
  const ents = useFetch('/api/v1/entities?page_size=200')
  const accts = useFetch('/api/v1/accounts?page_size=500')      // issue #87-2：币种下拉
  const inv = useFetch('/api/v1/investments')

  const [form, setForm] = useState({
    year: 1989, region: '欧洲', risk_lvl: 'R3', start_date: '1989-06-30',
    allocs: [{ entity_id: '', currency: '', amount: '', all: false }],
  })
  const [localErr, setLocalErr] = useState(null)

  const regionMap = regions.data || {}
  const entityOpts = (ents.data?.items || []).filter(e => e.type === 'person' || e.type === 'company')
  // issue #87-2：币种可选来自该主体真实账户池（active，排除关池只读终态 §6.6）
  const currenciesFor = (eid) => {
    const n = Number(eid)
    if (!n) return []
    const curMap = {}
    for (const a of (accts.data?.items || [])) {
      if (a.entity_id === n && a.status === 'active' && !curMap[a.currency]) curMap[a.currency] = true
    }
    return Object.keys(curMap)
  }

  const submit = useDataOp(() => inv.refresh())
  const redeem = useDataOp(() => inv.refresh())
  const unlock = useDataOp(() => inv.refresh())

  // 审计修复：年份变更时发生日联动（默认当年 06-30），避免跨年发生日算出 0 天/异常天数
  const setYear = (v) => setForm(f => ({
    ...f, year: v,
    start_date: (f.start_date || '').startsWith(String(v)) ? f.start_date : `${v}-06-30`,
  }))
  // 活期结息（§19.2 · 审计补齐）：2% 年化按日折，12-30 入账，同年幂等重跑
  const [demandYear, setDemandYear] = useState(2001)
  const demand = useDataOp()

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const setAlloc = (i, k, v) => setForm(f => ({
    ...f, allocs: f.allocs.map((a, idx) => idx === i ? { ...a, [k]: v } : a),
  }))
  // issue #87-2：选中主体后自动填首个可用币种（避免自由文本拼错大小写落服务端才报无账户）
  const pickEntity = (i, eid) => {
    const cs = currenciesFor(eid)
    setAlloc(i, 'entity_id', eid)
    setAlloc(i, 'currency', cs[0] || '')
  }
  const addAlloc = () => setForm(f => ({ ...f, allocs: [...f.allocs, { entity_id: '', currency: '', amount: '', all: false }] }))
  const delAlloc = (i) => setForm(f => ({ ...f, allocs: f.allocs.filter((_, idx) => idx !== i) }))

  const doSubmit = async () => {
    setLocalErr(null)
    const body = {
      year: Number(form.year), region: form.region, risk_lvl: form.risk_lvl,
      start_date: form.start_date,
      allocs: form.allocs
        .filter(a => a.entity_id)
        .map(a => ({ entity_id: Number(a.entity_id), currency: a.currency,
                     amount: a.all ? null : Number(a.amount), is_all: a.all })),
    }
    if (!body.allocs.length) { setLocalErr('至少填一个分配（主体+币种）'); return }
    await submit('/api/v1/investments', body)
  }

  const regionDisabled = regionMap[form.region] && form.year < regionMap[form.region].start_year

  // issue #125：已投年份（年+地区，锁定且未赎回）UI 置灰——防重投不再只靠服务端 409 兜底
  const investedSet = new Set((inv.data?.items || [])
    .filter(x => x.locked && !x.redeemed).map(x => `${x.year}:${x.region}`))
  const alreadyInvested = investedSet.has(`${Number(form.year)}:${form.region}`)

  return (
    <div className="cols2">
      <div className="panel">
        <h3>投资（一年一投 · 年末赎回）</h3>
        <p className="note">DESIGN §19.1–19.4 · UI 派生通道 · 失败整体拒绝（表单保留不变）</p>
        <div className="form">
          <div className="row">
            <Field label="年份"><input type="number" min={1947} max={2026} value={form.year}
              onChange={e => setYear(Number(e.target.value))} /></Field>
            <Field label="风险级">
              <select value={form.risk_lvl} onChange={e => set('risk_lvl', e.target.value)}>
                {RISK.map(r => <option key={r}>{r}</option>)}
              </select>
            </Field>
          </div>
          <Field label="地区（起始年下限）">
            <select value={form.region} onChange={e => set('region', e.target.value)}>
              {Object.entries(regionMap).map(([r, meta]) => (
                <option key={r} value={r}>{r}（{meta.start_year} 起·{meta.country}）</option>
              ))}
            </select>
          </Field>
          <Field label="投资发生日（计息起）">
            <input type="date" value={form.start_date} onChange={e => set('start_date', e.target.value)} />
          </Field>
          <div className="label" style={{ marginTop: 10 }}>分配（主体 × 币种 × 金额 / $全部）</div>
          {form.allocs.map((a, i) => (
            <div key={i} className="alloc-row">
              <select value={a.entity_id} onChange={e => pickEntity(i, e.target.value)} style={{ flex: 1 }}>
                <option value="">—主体—</option>
                {entityOpts.map(e => <option key={e.id} value={e.id}>{e.type}:{e.name}</option>)}
              </select>
              <select value={a.currency} style={{ width: 90 }}
                onChange={e => setAlloc(i, 'currency', e.target.value)}>
                <option value="">币种</option>
                {currenciesFor(a.entity_id).map(c => <option key={c}>{c}</option>)}
              </select>
              <input type="number" placeholder="金额" value={a.amount} disabled={a.all} style={{ width: 90 }}
                onChange={e => setAlloc(i, 'amount', e.target.value)} />
              <label className="chk"><input type="checkbox" checked={a.all}
                onChange={e => setAlloc(i, 'all', e.target.checked)} />全</label>
              <button className="ghost" onClick={() => delAlloc(i)}>✕</button>
            </div>
          ))}
          <div className="row">
            <button className="ghost" onClick={addAlloc}>+ 添加主体</button>
            <button className="primary" disabled={regionDisabled || alreadyInvested || submit.busy} onClick={doSubmit}>
              {submit.busy ? '提交中…' : '提交投资'}
            </button>
          </div>
          {regionDisabled && <div className="warn">年份低于 {form.region} 起始年下限 {regionMap[form.region].start_year}</div>}
          {alreadyInvested && (
            <div className="warn">该「年份+地区」已投且锁定（§6.7 一年一投）——请在右侧状态表先「解锁」再重输</div>
          )}
          <ErrorBox error={localErr || submit.error} />

          <h3 style={{ marginTop: 22 }}>活期结息（§19.2 · 2% 年化按日折）</h3>
          <p className="note">对全部 active 账户按台账逐日余额加权计息，当年 12-30 入账；同年重跑幂等覆盖</p>
          <div className="row">
            <Field label="结息年份"><input type="number" min={1947} max={2026} value={demandYear}
              onChange={e => setDemandYear(Number(e.target.value))} /></Field>
            <button className="primary" disabled={demand.busy} style={{ alignSelf: 'flex-end' }}
              onClick={() => demand.submit('/api/v1/demand-interest', { year: Number(demandYear) })}>
              {demand.busy ? '结息中…' : '活期结息'}
            </button>
          </div>
          {demand.last && (
            <div className="note">
              已入账 {demand.last.accounts} 个账户
              {Object.entries(demand.last.total_by_currency || {})
                .map(([c, v]) => ` · ${c} ${v}`).join('')}
            </div>
          )}
          <ErrorBox error={demand.error} />
        </div>
      </div>

      <div className="panel">
        <h3>投资状态（GET /investments）</h3>
        <p className="note">已投年份置灰锁定；「赎回」在 12-30 划回本金+收益、专款池清空（已赎回禁点）；「解锁」整笔抹除重输（issue #81/#82）</p>
        <table>
          <thead><tr><th>年</th><th>地区</th><th>R</th><th>发生日</th><th>分配</th><th>状态</th><th></th></tr></thead>
          <tbody>
            {(inv.data?.items || []).map(x => (
              <tr key={x.id}>
                <td>{x.year}</td><td>{x.region}</td><td>{x.risk_lvl}</td>
                <td className="mono">{x.start_date}</td>
                <td className="muted">{(x.allocs || []).map(a => `${a.currency}${a.is_all ? '(全部)' : a.amount}`).join('、')}</td>
                <td>{x.redeemed ? '已赎回' : (x.locked ? '已投·锁定' : '已解锁')}</td>
                <td>
                  <button className="ghost" disabled={redeem.busy || x.redeemed}
                    onClick={() => redeem.submit(`/api/v1/investments/${x.id}/redeem`, {})}>赎回</button>
                  <button className="ghost" disabled={unlock.busy || x.redeemed}
                    onClick={() => unlock.submit(`/api/v1/investments/${x.id}`, { locked: false }, { method: 'PATCH' })}>解锁</button>
                </td>
              </tr>
            ))}
            {!(inv.data?.items || []).length && <tr><td colSpan={7} className="note">暂无投资</td></tr>}
          </tbody>
        </table>
        <ErrorBox error={redeem.error || unlock.error} />
      </div>
    </div>
  )
}