import { useMemo, useState } from 'react'
import { useDataOp, useFetch } from './useDataOp'
import { ErrorBox, Field } from './ui'

/** F-P1-03/09 划拨/换汇屏：同币=划拨、跨币=换汇（需该年汇率），转出向后全链不破负。
 *  #143 备案：本屏为写操作表单，**有意**不随全局日历游标（asOf）联动——
 *  划拨年份由表单显式输入；余额校验以服务端该年 as-of 口径为准。 */
export default function Transfer({ calMax = 2026 }) {
  const accounts = useFetch('/api/v1/accounts?page_size=200')
  const entities = useFetch('/api/v1/entities?page_size=200')
  const op = useDataOp()

  const [form, setForm] = useState({
    year: 2001, source_account_id: '', target_entity_id: '', target_currency: '', amount: '',
  })
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const fx = useFetch(form.year ? `/api/v1/exchange-rates?year=${form.year}&page_size=500` : null)

  const acctOpts = accounts.data?.items || []
  const entOpts = (entities.data?.items || []).filter(e => e.type === 'person' || e.type === 'company')
  const src = acctOpts.find(a => a.id === Number(form.source_account_id))

  // issue #87-1：目标币种下拉 = 源币种（同币划拨）+ 该年与源币种有正向/反向汇率的方向
  const validTargets = useMemo(() => {
    const s = new Set()
    if (src?.currency) s.add(src.currency)
    for (const r of (fx.data?.items || [])) {
      if (r.from === src?.currency) s.add(r.to)
      if (r.to === src?.currency) s.add(r.from)          // 反向可用
    }
    return [...s].sort()
  }, [fx.data, src])
  // 源账户/年份变化后目标币种随之回落到合法首项（避免显示与提交不一致）
  const effTarget = validTargets.includes(form.target_currency) ? form.target_currency : (validTargets[0] || '')

  // 八轮审计 #189：表单级幂等键——挂载生成、仅成功提交后重置；
  // 双击/重试复用同一 nonce → 服务端查重第二次 skipped，不再双记账
  const [nonce, setNonce] = useState(() =>
    (crypto.randomUUID?.() || `${Date.now()}${Math.random()}`).replace(/-/g, '').slice(0, 12))
  const newNonce = () => setNonce(
    (crypto.randomUUID?.() || `${Date.now()}${Math.random()}`).replace(/-/g, '').slice(0, 12))

  const doSubmit = async () => {
    if (!form.source_account_id || !form.target_entity_id) return
    const res = await op.submit('/api/v1/transfers', {
      source_account_id: Number(form.source_account_id),
      target_entity_id: Number(form.target_entity_id),
      target_currency: effTarget || form.target_currency,
      amount: Number(form.amount),
      year: Number(form.year),
      nonce,
    })
    if (res.ok) {
      setForm(f => ({ ...f, amount: '' }))
      newNonce()
    }
    // skipped（幂等重放）也重置 nonce——用户需感知为一次新操作起点
    else if (res.data?.status === 'skipped') { newNonce() }
  }

  return (
    <div className="cols2">
      <div className="panel">
        <h3>划拨 / 换汇</h3>
        <p className="note">DESIGN §19.5 · 同币种=划拨（净 0）、跨币种=换汇（需该年汇率）· 转出後链不破负</p>
        <div className="form">
          <div className="row">
            <Field label="年份">
              <input type="number" min={1947} max={calMax} value={form.year}
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
              <select value={effTarget}
                style={{ width: 110 }}
                onChange={e => set('target_currency', e.target.value)}>
                <option value="">币种</option>
                {validTargets.map(c => <option key={c}>{c}</option>)}
              </select>
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
              {op.last.status === 'skipped' || op.last.operation === '重放跳过'
                ? '重复提交已跳过（幂等保护）——该笔此前已入账'
                : `${op.last.operation === '换汇' ? '换汇' : '划拨'}：${op.last.amount} ${op.last.source_currency} → ${op.last.target_amount} ${op.last.target_currency}`}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}