import { useEffect, useState } from 'react'
import { useDataOp, useFetch } from './useDataOp'
import { ErrorBox } from './ui'

const LEVELS = ['', 'block', 'warn', 'error']
const LEVEL_LABEL = { block: '🔴 拦截', warn: '🟡 警告', error: '⛔ 解析失败' }
const JOB_BADGE = { pending: '⏳ 待执行', running: '🏃 执行中', done: '✅ 完成', failed: '❌ 失败' }

/** F-A5 导入状态屏（issue #123/#138）：ingest_report + 异步任务（重算/外部导入）触发与进度。 */
export default function ImportStatus() {
  const [level, setLevel] = useState('')
  const q = new URLSearchParams({ page_size: '200' })
  if (level) q.set('level', level)
  const rep = useFetch(`/api/v1/ingest-reports?${q.toString()}`)
  const rows = rep.data?.items || []

  // issue #138：异步 job 资源——触发重算 + 轮询任务状态
  const [tick, setTick] = useState(0)
  const jobs = useFetch(`/api/v1/recompute-jobs?limit=10&offset=0&t=${tick}`)
  const imports = useFetch(`/api/v1/import-jobs?limit=10&offset=0&t=${tick}`)
  const op = useDataOp(() => setTimeout(() => setTick(t => t + 1), 300))
  const busyJobs = [...(jobs.data?.items || []), ...(imports.data?.items || [])]
    .some(j => j.status === 'pending' || j.status === 'running')

  useEffect(() => {
    if (!busyJobs) return undefined
    const t = setInterval(() => setTick(x => x + 1), 1500)
    return () => clearInterval(t)
  }, [busyJobs])

  const triggerRecompute = () => op.submit('/api/v1/recompute-jobs',
    { start_year: 1947, reason: 'ui-手动触发' })
  const triggerCompanyImport = () => op.submit('/api/v1/import-jobs',
    { provider: 'company-info' })

  return (
    <div className="panel">
      <h3>导入状态 / 冲突报告</h3>
      <p className="note">
        §11.4：硬拦截文件不入库，由数据调整员修源文件后重导；警告=已入库但需关注。
        CLI：<span className="mono">python -m app.ingest.main ingest --env …</span>
      </p>

      {/* issue #138：异步任务资源（§14.2） */}
      <h4>异步任务（#138 · §14.2）</h4>
      <div className="row" style={{ marginBottom: 8 }}>
        <button className="primary" disabled={op.busy || busyJobs} onClick={triggerRecompute}>
          触发全量重算（1947 起）
        </button>
        <button className="ghost" disabled={op.busy || busyJobs} onClick={triggerCompanyImport}>
          导入公司信息（外部 API①）
        </button>
        {busyJobs && <span className="note">⏳ 有任务执行中…（1.5s 自动刷新）</span>}
      </div>
      <ErrorBox error={op.error} />
      <table style={{ marginBottom: 16 }}>
        <thead><tr><th>ID</th><th>类型</th><th>状态</th><th>参数 / 结果</th><th>时间</th></tr></thead>
        <tbody>
          {(jobs.data?.items || []).map(j => (
            <tr key={`r${j.id}`}>
              <td className="mono">R{j.id}</td><td>重算</td>
              <td>{JOB_BADGE[j.status] || j.status}</td>
              <td style={{ fontSize: 12 }}>
                自 {j.start_year} 起{j.reason ? ` · ${j.reason}` : ''}
                {j.health_error && <span className="badge warn"> health 异常</span>}
              </td>
              <td className="muted" style={{ fontSize: 11 }}>{(j.finished_at || j.created_at || '').replace('T', ' ').slice(0, 19)}</td>
            </tr>
          ))}
          {(imports.data?.items || []).map(j => (
            <tr key={`i${j.id}`}>
              <td className="mono">I{j.id}</td>
              <td>{j.provider === 'company-info' ? '公司导入①' : '用工成本②'}</td>
              <td>{JOB_BADGE[j.status] || j.status}</td>
              <td style={{ fontSize: 12 }}>
                {JSON.stringify(j.payload || {})}
                {j.error && ` · ${j.error}`}
                {j.result && ` · ${JSON.stringify(j.result).slice(0, 120)}`}
              </td>
              <td className="muted" style={{ fontSize: 11 }}>{(j.finished_at || j.created_at || '').replace('T', ' ').slice(0, 19)}</td>
            </tr>
          ))}
          {!(jobs.data?.items?.length) && !(imports.data?.items?.length) &&
            <tr><td colSpan={5} className="note">暂无任务记录</td></tr>}
        </tbody>
      </table>

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
