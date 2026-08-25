import { useState } from 'react'
import { useFetch } from './useDataOp'

const LEVELS = ['', 'block', 'warn', 'error']
const LEVEL_LABEL = { block: '🔴 拦截', warn: '🟡 警告', error: '⛔ 解析失败' }

/** F-A5 导入状态屏（issue #123）：ingest_report（冲突拦截/软警告/解析失败）+ 最近批次。 */
export default function ImportStatus() {
  const [level, setLevel] = useState('')
  const q = new URLSearchParams({ page_size: '200' })
  if (level) q.set('level', level)
  const rep = useFetch(`/api/v1/ingest-reports?${q.toString()}`)
  const rows = rep.data?.items || []

  return (
    <div className="panel">
      <h3>导入状态 / 冲突报告</h3>
      <p className="note">
        §11.4：硬拦截文件不入库，由数据调整员修源文件后重导；警告=已入库但需关注。
        CLI：<span className="mono">python -m app.ingest.main ingest --env …</span>
      </p>
      <div className="row" style={{ marginBottom: 8 }}>
        {LEVELS.map(l => (
          <button key={l || 'all'} className={`ghost ${level === l ? 'primary' : ''}`}
            onClick={() => setLevel(l)}>{l ? LEVEL_LABEL[l] : '全部'}</button>
        ))}
        <span className="note" style={{ marginLeft: 8 }}>共 {rep.data?.total ?? '—'} 条</span>
      </div>
      <table>
        <thead><tr><th>级别</th><th>规则</th><th>文件</th><th>行</th><th>明细</th><th>时间</th></tr></thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.id}>
              <td>{LEVEL_LABEL[r.level] || r.level}</td>
              <td>{r.rule || '—'}</td>
              <td className="mono" style={{ fontSize: 12 }}>{r.file}</td>
              <td className="num">{r.line ?? '—'}</td>
              <td style={{ fontSize: 12 }}>{r.detail}</td>
              <td className="muted" style={{ fontSize: 11 }}>{(r.created_at || '').replace('T', ' ').slice(0, 19)}</td>
            </tr>
          ))}
          {!rows.length && <tr><td colSpan={6} className="note">暂无记录 ✓（无拦截/警告/失败）</td></tr>}
        </tbody>
      </table>
    </div>
  )
}
