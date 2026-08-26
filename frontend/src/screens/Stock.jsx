import { useState } from 'react'
import { useFetch, useDataOp } from './useDataOp'
import { ErrorBox } from './ui'

const FMT = (n, unit = '') => (n != null ? (+n).toLocaleString() + unit : '—')

/**
 * 股票事件屏（F-P2-02 · §19.6）：持仓明细 + 导入事件关联 + 手动买入/卖出/分红/被动抬升。
 * 写操作后传（recompute+快照）由后端同一请求内完成，前端不另发。
 */
export default function Stock() {
  const [refresh, setRefresh] = useState(0)
  const positions = useFetch(`/api/v1/stock-events/positions?refresh=${refresh}`)
  const events = useFetch(`/api/v1/stock-events/events?refresh=${refresh}`)
  const accounts = useFetch('/api/v1/accounts?page_size=200')
  const op = useDataOp(() => setRefresh(r => r + 1))

  // 关联状态
  const [assocId, setAssocId] = useState(null)
  const [entSel, setEntSel] = useState('')
  const [accSel, setAccSel] = useState('')

  // 手动动作表单
  const [form, setForm] = useState({ company: '', date: '2018-12-30', unit_price: '', shares: '', per_share: '', sell_price: '', pct: '', event_id: 'ui-stock-1', entity_id: '', account_id: '' })
  const [action, setAction] = useState('buy')

  const doAssociate = () => {
    if (!entSel || !accSel) return
    op.submit('/api/v1/stock-events/associate', { stock_event_id: assocId, entity_id: Number(entSel), account_id: Number(accSel) })
    setAssocId(null); setEntSel(''); setAccSel('')
  }

  const doAction = () => {
    const body = {
      entity_id: Number(form.entity_id), account_id: Number(form.account_id),
      company: form.company, date: form.date, event_id: form.event_id || 'ui-stock-1',
      unit_price: form.unit_price ? Number(form.unit_price) : undefined,
      shares: form.shares ? Number(form.shares) : undefined,
      sell_price: form.sell_price ? Number(form.sell_price) : undefined,
      per_share: form.per_share ? Number(form.per_share) : undefined,
      pct: form.pct ? Number(form.pct) : undefined,
    }
    op.submit(`/api/v1/stock-events/${action}`, body)
  }

  const pos = positions.data?.items || []
  const evts = events.data?.items || []
  const accts = (accounts.data?.items || [])
  const entities = [...new Set(accts.map(a => a.entity_id))].filter(Boolean)
  const buyEvents = evts.filter(e => e.event_type === 'buy' && !e.linked)

  const inp = (k, w = 90) => (
    <input style={{ width: w }} value={form[k] || ''} placeholder={k}
      onChange={e => setForm(f => ({ ...f, [k]: e.target.value }))} />
  )

  return (
    <div className="screen">
      <div className="panel">
        <h3>股票事件</h3>
        <p className="note">持仓市值并入总资产（总资产=现金+专款池+股票市值）；F-P2-02</p>
        <ErrorBox error={op.error} />

        <h4>当前持仓</h4>
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th>标的</th><th>Ticker</th><th>批次</th><th>总股数</th><th>市值(USD)</th><th>占比</th><th>Entity</th></tr></thead>
          <tbody>
            {pos.map((p, i) => (
              <tr key={i}>
                <td>{p.company}</td><td>{p.ticker || '—'}</td><td>{p.batches}</td>
                <td>{FMT(p.total_shares)}</td><td>{FMT(p.market_value, '')}</td>
                <td>{p.pct != null ? p.pct + '%' : '—'}</td><td>ent#{p.entity_id}</td>
              </tr>
            ))}
            {!pos.length && <tr><td colSpan="7" className="note">暂无持仓</td></tr>}
          </tbody>
        </table>

        <h4>导入的待关联 buy 事件</h4>
        {assocId && (() => {
          const evCur = (events.data?.items || []).find(e => e.id === assocId)?.currency || 'USD'
          return (
            <div style={{ margin: '8px 0' }}>
              <span className="note">选择主体 & 账户（同币种 {evCur} · PRD §6.8）：</span>
              <select value={entSel} onChange={e => setEntSel(e.target.value)}>
                <option value="">— 主体 —</option>
                {entities.map(e => <option key={e} value={e}>entity#{e}</option>)}
              </select>
              <select value={accSel} onChange={e => setAccSel(e.target.value)}>
                <option value="">— 账户 —</option>
                {accts.filter(a => a.currency === evCur).map(a => <option key={a.id} value={a.id}>acct#{a.id} {a.currency}</option>)}
              </select>
              {accts.length > 0 && accts.every(a => a.currency !== evCur) &&
                <span className="warn">无同币种账户可关联</span>}
              <button className="primary" disabled={!entSel || !accSel || op.busy} onClick={doAssociate}>确认关联</button>
              <button className="ghost" onClick={() => { setAssocId(null); setEntSel(''); setAccSel('') }}>取消</button>
            </div>
          )
        })()}
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th>公司</th><th>日期</th><th>股数</th><th>单价</th><th>金额(万USD)</th><th>占比</th><th></th></tr></thead>
          <tbody>
            {buyEvents.map(e => (
              <tr key={e.id}>
                <td>{e.company}{e.ticker ? ` (${e.ticker})` : ''}</td><td>{e.date}</td>
                <td>{FMT(e.shares)}</td><td>{FMT(e.unit_price)}</td><td>{FMT(e.amount, '万')}</td>
                <td>{e.pct != null ? e.pct + '%' : '—'}</td>
                <td><button className="ghost" onClick={() => setAssocId(e.id)}>关联</button></td>
              </tr>
            ))}
            {!buyEvents.length && <tr><td colSpan="7" className="note">无待关联 buy 事件（可手动买入）</td></tr>}
          </tbody>
        </table>

        <h4>手动动作</h4>
        <div>
          <select value={action} onChange={e => setAction(e.target.value)}>
            <option value="buy">买入 buy</option>
            <option value="sell">卖出 sell</option>
            <option value="dividend">分红 dividend</option>
            <option value="passive-uplift">被动抬升</option>
          </select>
          {' '}{inp('entity_id', 60)} {inp('account_id', 60)} {inp('company', 120)} {inp('date', 120)}
          {action === 'buy' && <>{inp('unit_price', 80)} {inp('shares', 100)}</>}
          {action === 'sell' && <>{inp('sell_price', 80)} {inp('shares', 100)}</>}
          {action === 'dividend' && inp('per_share', 80)}
          {action === 'passive-uplift' && inp('pct', 60)}
          {inp('event_id', 130)}
          <button className="primary" disabled={op.busy} onClick={doAction}>执行 {action}</button>
        </div>
        <p className="note">分红需现持仓（每股×股数→现金）；卖出按 FIFO，超卖 422；event_id 唯一幂等。</p>
      </div>
    </div>
  )
}