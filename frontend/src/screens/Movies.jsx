import { useState } from 'react'
import { useFetch, useDataOp } from './useDataOp'
import { ErrorBox } from './ui'

const FMT = (n) => (n != null ? '$' + (n / 1e6).toFixed(1) + 'M' : '—')

/**
 * 电影事件屏（F-P2-01 · §19.6）：导入的事件 + 同币种手动关联到账户（写 ledger）。
 * 上：未关联列表 + 关联；下：已关联事件 + 解关联。
 */
export default function Movies() {
  const [refresh, setRefresh] = useState(0)
  const all = useFetch(`/api/v1/movie-events?refresh=${refresh}`)
  const op = useDataOp(() => { setRefresh(r => r + 1) })
  const [linking, setLinking] = useState(null)          // MovieEvent id
  const [accSel, setAccSel] = useState('')
  const accounts = useFetch('/api/v1/accounts?page_size=200')

  const items = all.data?.items || []
  const unlinked = items.filter(m => !m.linked)
  const linkedItems = items.filter(m => m.linked)

  const doLink = () => {
    if (!accSel) return
    op.submit(`/api/v1/movie-events/${linking}/link`, { account_id: Number(accSel) })
    setLinking(null); setAccSel('')
  }

  return (
    <div className="screen">
      <div className="panel">
        <h3>电影事件</h3>
        <p className="note">共 {items.length} 部；导入后不关联账户，同币种手动关联 → 现金入账（F-P2-01）</p>
        <ErrorBox error={op.error} />

        {linking && (
          <div style={{ margin: '8px 0' }}>
            <span className="note">选择关联账户（同币种 {linking.currency || 'USD'} · PRD §6.8）：</span>
            <select value={accSel} onChange={e => setAccSel(e.target.value)}>
              <option value="">— 选账户 —</option>
              {(accounts.data?.items || []).filter(a => a.currency === (linking.currency || 'USD') && a.status !== 'closed').map(a => (
                <option key={a.id} value={a.id}>acct#{a.id} {a.currency}{a.status === 'closed' ? ' (已关)' : ''}</option>
              ))}
            </select>
            {!(accounts.data?.items || []).some(a => a.currency === (linking.currency || 'USD') && a.status !== 'closed') &&
              <span className="warn">无同币种 active/closed 账户可关联</span>}
            <button className="primary" disabled={!accSel || op.busy} onClick={doLink}>确认关联</button>
            <button className="ghost" onClick={() => { setLinking(null); setAccSel('') }}>取消</button>
          </div>
        )}

        <h4>未关联</h4>
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th>片名</th><th>地区</th><th>投资额</th><th>本金返还</th><th>分红</th><th></th></tr></thead>
          <tbody>
            {unlinked.map(m => (
              <tr key={m.id}>
                <td>{m.title}</td><td>{m.region || '—'}</td>
                <td>{FMT(m.investment_total)}</td>
                <td>{FMT(m.principal_return_amount)}{m.principal_return_date ? ` @${m.principal_return_date}` : ''}</td>
                <td>{FMT(m.dividends_total)}</td>
                <td><button className="ghost" onClick={() => setLinking(m.id)}>关联</button></td>
              </tr>
            ))}
            {!unlinked.length && <tr><td colSpan="6" className="note">无未关联事件</td></tr>}
          </tbody>
        </table>

        <h4>已关联</h4>
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th>片名</th><th>账户</th><th>分红</th><th></th></tr></thead>
          <tbody>
            {linkedItems.map(m => (
              <tr key={m.id}>
                <td>{m.title}</td><td>acct#{m.linked_account_id}</td><td>{FMT(m.dividends_total)}</td>
                <td><button className="ghost" disabled={op.busy} onClick={() => op.submit(`/api/v1/movie-events/${m.id}/unlink`, {})}>解关联</button></td>
              </tr>
            ))}
            {!linkedItems.length && <tr><td colSpan="4" className="note">无已关联事件</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}