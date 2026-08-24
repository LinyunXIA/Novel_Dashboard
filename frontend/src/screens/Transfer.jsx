import { useState } from 'react'
import { useDataOp, useFetch } from './useDataOp'
import { ErrorBox, Field } from './ui'

/** F-P1-03/09 划拨/换汇屏：同币=划拨、跨币=换汇（需该年汇率），转出向后全链不破负。 */
export default function Transfer() {
  const accounts = useFetch('/api/v1/accounts?page_size=200')
  const entities = useFetch('/api/v1/entities?page_size=200')
  const op = useDataOp()

  const [form, setForm] = useState({
    year: 2001, source_account_id: '', target_entity_id: '', target_currency: 'USD', amount: '',
  })
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const acctOpts = accounts.data?.items || []
  const entOpts = (entities.data?.items || []).filter(e => e.type === 'person' || e.type === 'company')
  const src = acctOpts.find(a => a.id === Number(form.source_account_id))

  const doSubmit = async () => {
    if (!form.source_account_id || !form.target_entity_id) return
    const res = await op.submit('/api/v1/transfers', {
      source_account_id: Number(form.source_account_id),
      target_entity_id: Number(form.target_entity_id),
      target_currency: form.target_currency,
      amount: Number(form.amount),
      year: Number(form.year),
    })
    if (res.ok) { setForm(f => ({ ...f, amount: '' })) }
  }

  return (
    <div className="cols2">
      <div className="panel">
        <h3>划拨 / 换汇</h3>
        <p className="note">DESIGN §19.5 · 同币种=划拨（净 0）、跨币种=换汇（需该年汇率）· 转出後链不破负</p>
        <div className="form">
          <div className="row">
            <Field label="年份">
              <input type="number" min={1947} max={2026} value={form.year}
                onChange={e => set('year', Number(e.target.value))} />
            </Field>
          </div>
          <Field label="源账户（主体·币种 · 关池不可选）">
            <select value={form.source_account_id} onChange={e => set('source_account_id', e.target.value)}>
              <option value="">—选择源账户—</option>
              {acctOpts.map(a => (
                <option key={a.id} value={a.id} disabled={a.status === 'closed'}
                  title={a.status === 'closed' ? `§6.6 关池后只读终态（${a.closed_on || ''}）` : ''}>
                  #{a.id} · entity{a.entity_id} · {a.currency}{a.status === 'closed' ? '（关池·禁选）' : ''}
                </option>
              ))}
            </select>
          </Field>
          <div className="row">
            <Field label="目标主体">
              <select value={form.target_entity_id} onChange={e => set('target_entity_id', e.target.value)}>
                <option value="">—目标主体—</option>
                {entOpts.map(e => <option key={e.id} value={e.id}>{e.type}:{e.name}</option>)}
              </select>
            </Field>
            <Field label="目标币种">
              <input value={form.target_currency} style={{ width: 90 }}
                onChange={e => set('target_currency', e.target.value)} />
            </Field>
          </div>
          <Field label="金额（源币）">
            <input type="number" value={form.amount} onChange={e => set('amount', e.target.value)} />
          </Field>
          <div className="row">
            <button className="primary" disabled={op.busy || !form.source_account_id || !form.target_entity_id}
              onClick={doSubmit}>{op.busy ? '提交中…' : '提交'}</button>
            <span className="note">{src ? `源币种 ${src.currency}` : ''}</span>
          </div>
          <ErrorBox error={op.error} />
          {op.last && (
            <div className="result">
              {op.last.operation === '换汇' ? '换汇' : '划拨'}：{op.last.amount} {op.last.source_currency} → {op.last.target_amount} {op.last.target_currency}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}