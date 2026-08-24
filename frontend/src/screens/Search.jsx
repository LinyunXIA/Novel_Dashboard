import { useState } from 'react'
import { useFetch } from './useDataOp'

/**
 * 统一搜索屏（F-P1-08 · DESIGN §18）：输入问题 → GET /api/v1/search → 只渲染最终 answer。
 * §18.6：硬剪裁——不展示推理/步骤/hits，仅 answer 字段。
 */
export default function Search() {
  const [q, setQ] = useState('')
  const [asked, setAsked] = useState('')
  const res = useFetch(asked ? `/api/v1/search?q=${encodeURIComponent(asked)}` : null)
  const answer = res.data?.answer

  const ask = () => { if (q.trim()) setAsked(q.trim()) }

  return (
    <div className="screen">
      <div className="panel">
        <h3>统一搜索</h3>
        <p className="note">向本地知识库提问，返回最终答案（不展示推理过程与来源）。</p>
        <div style={{ display: 'flex', gap: 8, margin: '8px 0' }}>
          <input style={{ flex: 1 }} value={q} placeholder="如：祖母哪年去世 / 皮克斯收购迪士尼"
            onChange={e => setQ(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') ask() }} />
          <button className="primary" onClick={ask} disabled={!q.trim()}>搜索</button>
        </div>
        {res.err && <p className="note" style={{ color: 'var(--crit)' }}>{String(res.err)}</p>}
        {res.data?.detail && <p className="note" style={{ color: 'var(--crit)' }}>{res.data.detail}</p>}
        {res.data && !res.data.detail && (
          <div style={{ marginTop: 12, lineHeight: 1.7 }}>
            {answer ? <p style={{ fontSize: 14 }}>{answer}</p>
                    : <p className="note">暂无答案（可先运行 search-index 建索引）。</p>}
          </div>
        )}
      </div>
    </div>
  )
}