import { useState } from 'react'
import { useFetch, useDataOp } from './useDataOp'
import { ErrorBox, Field } from './ui'

/**
 * 编年史屏（F-P2-05 · §12/§6.4）：overlay 增改删 + 差异/重置回源/以源为最新。
 * 用户覆盖行可编辑；系统行（投资/划拨 overlay）只读；源行可发起覆盖编辑。
 */
export default function Timeline({ asOf }) {
  const [refresh, setRefresh] = useState(0)
  // issue #121：全局日历游标接入（§14.2 ?as_of= 已发生事件）
  const asOfQ = asOf ? `&as_of=${asOf}` : ''
  const merged = useFetch(`/api/v1/timeline-events?page_size=500${asOfQ}&refresh=${refresh}`)
  const diff = useFetch(`/api/v1/timeline-events/overlay/diff?refresh=${refresh}`)
  const op = useDataOp(() => setRefresh(r => r + 1))

  const [form, setForm] = useState({ event_year: 1990, event_date: '', title: '', note: '', decade: '' })
  const [editingKey, setEditingKey] = useState(null)   // 覆盖行 (year:title) 编辑态；'new' 新增

  const items = merged.data?.items || []
  const diffs = diff.data?.items || []
  const diffStatus = {}
  for (const d of diffs) {
    diffStatus[d.key] = d
  }

  const startCreate = (pre) => {
    setEditingKey('new')
    setForm({ event_year: pre?.event_year || 1990, event_date: pre?.event_date || '',
             title: pre?.title || '', note: pre?.note || '', decade: pre?.decade || '' })
  }
  const startEdit = (row) => {
    setEditingKey(`${row.event_year}:${row.title}`)
    setForm({ event_year: row.event_year, event_date: row.event_date || '',
             title: row.title, note: row.note || '', decade: row.decade || '' })
  }
  const cancel = () => { setEditingKey(null) }

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const save = () => {
    const body = {
      event_year: Number(form.event_year), event_date: form.event_date || null,
      title: form.title, note: form.note || null, decade: form.decade || null,
    }
    if (editingKey === 'new') {
      op.submit('/api/v1/timeline-events', body)
    } else {
      // 用标题/year 反查覆盖行 id → PATCH
      const row = items.find(r => r.editable && `${r.event_year}:${r.title}` === editingKey)
      if (row) op.submit(`/api/v1/timeline-events/${row.id}`, body, { method: 'PATCH' })
    }
    cancel()
  }

  return (
    <div className="screen">
      <div className="panel">
        <h3>编年史（覆盖层编辑）</h3>
        <p className="note">F-P2-05 · overlay 增改删 · 差异/重置回源/以源为最新；系统行（投资/划拨）只读</p>
        <ErrorBox error={op.error} />

        {editingKey !== null && (
          <div className="form">
            <h4>{editingKey === 'new' ? '新增覆盖条目' : `编辑覆盖条目 ${editingKey}`}</h4>
            <div className="row">
              <Field label="年份"><input type="number" min={1947} max={2026} value={form.event_year}
                onChange={e => set('event_year', Number(e.target.value))} /></Field>
              <Field label="日期"><input type="date" value={form.event_date}
                onChange={e => set('event_date', e.target.value)} /></Field>
            </div>
            <Field label="标题"><input value={form.title} onChange={e => set('title', e.target.value)} /></Field>
            <Field label="备注"><textarea rows={2} value={form.note} onChange={e => set('note', e.target.value)} /></Field>
            <Field label="decade"><input value={form.decade} placeholder="如 1990s"
              onChange={e => set('decade', e.target.value)} /></Field>
            <div className="row">
              <button className="primary" disabled={!form.title || op.busy} onClick={save}>保存</button>
              <button className="ghost" onClick={cancel}>取消</button>
            </div>
          </div>
        )}

        <h4>生效条目（每 key 一行，覆盖行优先）</h4>
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th>年</th><th>日期</th><th>标题</th><th>备注</th><th>类型</th><th></th></tr></thead>
          <tbody>
            {items.map(r => {
              const isUser = r.editable
              const st = r.overlay_status
              return (
                <tr key={r.id}>
                  <td>{r.event_year}</td>
                  <td className="mono">{r.event_date || '—'}</td>
                  <td>{r.title}</td>
                  <td className="muted">{r.note || ''}</td>
                  <td>
                    {r.system ? <span className="badge">系统·只读</span>
                      : isUser ? <span className="badge">
                          {st === 'unchanged' ? '已同步源' : `覆盖${st === 'new' ? '·新' : ''}`}</span>
                      : <span className="badge">源</span>}
                    {r.has_source && <span className="note"> (来自源)</span>}
                  </td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    {isUser ? (
                      <>
                        <button className="ghost" onClick={() => startEdit(r)}>编辑</button>
                        <button className="ghost" onClick={() => op.submit(`/api/v1/timeline-events/${r.id}/overlay/restore`, {})}>重置回源</button>
                        <button className="ghost" onClick={() => op.submit(`/api/v1/timeline-events/${r.id}/overlay/source-as-latest`, {})}>以源为最新</button>
                        <button className="ghost" onClick={() => op.submit(`/api/v1/timeline-events/${r.id}`, {}, { method: 'DELETE' })}>删除</button>
                      </>
                    ) : !r.system ? (
                      <button className="ghost" onClick={() => startCreate(r)}>覆盖编辑</button>
                    ) : <span className="muted">—</span>}
                  </td>
                </tr>
              )
            })}
            {!items.length && <tr><td colSpan="6" className="note">暂无编年史条目</td></tr>}
          </tbody>
        </table>
        <div style={{ marginTop: 8 }}>
          <button className="primary" onClick={() => startCreate()}>+ 新增覆盖条目</button>
        </div>

        <h4 style={{ marginTop: 18 }}>覆盖层 vs 源差异（{diffs.length}）</h4>
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th>key</th><th>状态</th><th>变更字段</th></tr></thead>
          <tbody>
            {diffs.map(d => (
              <tr key={d.key}>
                <td>{d.key}</td>
                <td>{d.status === 'new' ? '新增' : d.status === 'modified' ? '已修改' : '一致'}</td>
                <td className="muted">{(d.changed_fields || []).join(', ')}</td>
              </tr>
            ))}
            {!diffs.length && <tr><td colSpan="3" className="note">无覆盖差异</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}