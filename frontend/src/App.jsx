import { useEffect, useState } from 'react'
import Dashboard from './screens/Dashboard'
import Invest from './screens/Invest'
import Transfer from './screens/Transfer'
import Returns from './screens/Returns'
import Finance from './screens/Finance'
import Graph from './screens/Graph'
import { ErrorBox } from './screens/ui'
import { useDataOp } from './screens/useDataOp'
import LaborCost from './screens/LaborCost'
import Movies from './screens/Movies'
import Stock from './screens/Stock'
import Search from './screens/Search'
import SourceDiff from './screens/SourceDiff'
import Timeline from './screens/Timeline'
import Health from './screens/Health'
import ImportStatus from './screens/ImportStatus'

const TABS = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'invest', label: '投资' },
  { key: 'transfer', label: '划拨/换汇' },
  { key: 'returns', label: '收益曲线' },
  { key: 'finance', label: '财务收支' },
  { key: 'labor', label: '加薪规则/用工成本' },
  { key: 'movies', label: '电影事件' },
  { key: 'stock', label: '股票事件' },
  { key: 'persons', label: '人物图谱' },
  { key: 'companies', label: '公司图谱' },
  { key: 'graphall', label: '全图谱' },
  { key: 'timeline', label: '编年史' },
  { key: 'diff', label: '版本/diff' },
  { key: 'search', label: '搜索' },
  { key: 'health', label: '健康校验' },
  { key: 'imports', label: '导入状态' },
]

export default function App() {
  const [active, setActive] = useState('dashboard')
  const [asOf, setAsOf] = useState('2001-12-30')
  const [health, setHealth] = useState(null)

  useEffect(() => {
    fetch('/api/v1/health').then(r => r.json()).then(setHealth).catch(() => setHealth(null))
  }, [])

  return (
    <div className="viz">
      <header className="topbar">
        <div className="brand">
          <h1>网文创作数据 Dashboard</h1>
          <span className="env">F-P1 · 交互数据操作 + 只读视图 · {health === null ? 'API 未连接' : 'API 已连接'}</span>
        </div>
      </header>

      <div className="panel calendar-bar">
        <span className="label">全局日历游标 · 截至日期（全 App 生效）</span>
        <input type="date" value={asOf} min="1947-01-01" max="2026-12-31"
          onChange={e => setAsOf(e.target.value)} />
        <span className="mono">{asOf}</span>
      </div>

      {/* issue #122/#140：重算完成非阻断横幅（§9.3 / F-U3），含「查看影响」入口 */}
      <NotificationsBanner onShowImpact={() => setActive('health')} />

      <nav className="nav">
        {TABS.map(t => (
          <button key={t.key} className={`tab ${active === t.key ? 'active' : ''}`}
            onClick={() => setActive(t.key)}>{t.label}</button>
        ))}
      </nav>

      <main className="screen">
        {active === 'dashboard' && <Dashboard asOf={asOf} />}
        {/* issue #121：数据屏以 asOf 作 key——游标变动即重挂载重新拉取，全 App 联动。
            投资/划拨为写操作表单，有意不随游标联动（#143 备案，见各屏 docstring） */}
        {active === 'invest' && <Invest asOf={asOf} />}
        {active === 'transfer' && <Transfer asOf={asOf} />}
        {active === 'returns' && <Returns key={`r-${asOf}`} asOf={asOf} />}
        {active === 'finance' && <Finance key={`f-${asOf}`} asOf={asOf} />}
        {active === 'labor' && <LaborCost />}
        {active === 'movies' && <Movies />}
        {active === 'stock' && <Stock />}
        {active === 'persons' && <Graph url="/api/v1/graph/persons" />}
        {active === 'companies' && <CompanyGraph />}
        {active === 'graphall' && <Graph url="/api/v1/graph/all" />}
        {active === 'timeline' && <Timeline key={`t-${asOf}`} asOf={asOf} />}
        {active === 'search' && <Search asOf={asOf} />}
        {active === 'diff' && <SourceDiff />}
        {active === 'health' && <Health />}
        {active === 'imports' && <ImportStatus />}
      </main>
    </div>
  )
}

/**
 * 重算通知横幅（issue #122/#140 · DESIGN §9.3 / PRD F-U3）：
 * 轮询 GET /notifications（默认仅未读）→ recompute-done 弹非阻断横幅，
 * 附 payload.health 摘要（#120）+「查看影响」按钮（跳健康校验屏，§9.3）
 * + crit 优先的 findings 预览（#140，payload.health_findings）；
 * 「知道了」PATCH 标记已读。
 */
function NotificationsBanner({ onShowImpact }) {
  const [items, setItems] = useState([])

  useEffect(() => {
    let alive = true
    const load = () => fetch('/api/v1/notifications')
      .then(r => r.json())
      .then(d => { if (alive) setItems((d.items || []).filter(n => !n.read_at)) })
      .catch(() => {})
    load()
    const timer = setInterval(load, 15000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  if (!items.length) return null

  const ack = (id) => fetch(`/api/v1/notifications/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ read_at: 'now' }),
  }).finally(() => setItems(xs => xs.filter(x => x.id !== id)))

  const healthText = (h) => {
    if (!h) return ''
    const bad = Object.entries(h).filter(([, v]) => v > 0)
    return bad.length ? `健康：${bad.map(([k, v]) => `${k}:${v}`).join(' ')}` : '健康：全部 ✓'
  }

  return (
    <div className="panel banner" role="status">
      {items.map(n => {
        const findings = n.payload?.health_findings || []
        const total = n.payload?.health_findings_total ?? findings.length
        return (
          <div key={n.id} style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <span>🔔 {n.message}{healthText(n.payload?.health) && ` · ${healthText(n.payload.health)}`}</span>
            <button className="ghost" onClick={() => onShowImpact?.()}>查看影响</button>
            <button className="ghost" onClick={() => ack(n.id)}>知道了</button>
            {!!findings.length && (
              <details style={{ width: '100%' }}>
                <summary className="note">受影响明细（前 {findings.length} / 共 {total}）</summary>
                <pre className="mono" style={{ maxHeight: 200, overflow: 'auto', background: '#fff' }}>
                  {findings.map((f, i) =>
                    `[${f.rule}/${f.level}] ${f.location}: ${f.detail}`).join('\n')}
                </pre>
              </details>
            )}
          </div>
        )
      })}
    </div>
  )
}

/**
 * 公司图谱屏（F-P1-05 / F-U7 · issue #138）：「获取/导入公司」改走 import-jobs
 * 异步资源——POST /api/v1/import-jobs{provider:'company-info'} 建任务 → 轮询
 * GET /api/v1/import-jobs/{id} 至 done/failed → done 取 result.stats 刷新图谱。
 */
function CompanyGraph() {
  const [refreshKey, setRefreshKey] = useState(0)
  const [stats, setStats] = useState(null)
  const [jobId, setJobId] = useState(null)
  const [jobError, setJobError] = useState(null)
  const { submit, busy, error } = useDataOp(data => {
    setJobError(null)
    if (data?.id) setJobId(data.id)
  })

  useEffect(() => {
    if (!jobId) return undefined
    let alive = true
    const t = setInterval(() => {
      fetch(`/api/v1/import-jobs/${jobId}`).then(r => r.json()).then(j => {
        if (!alive) return
        if (j.status === 'done') {
          clearInterval(t)
          setStats(j.result?.stats || null)
          setJobId(null)
          setRefreshKey(k => k + 1)
        } else if (j.status === 'failed') {
          clearInterval(t)
          setJobError(j.error || '导入任务失败')
          setJobId(null)
        }
      }).catch(() => {})
    }, 1200)
    return () => { alive = false; clearInterval(t) }
  }, [jobId])

  const statsText = stats
    ? `已处理 ${stats.companies} 家公司 · 新增 ${stats.companies_created} 家 · 股权关系 ${stats.rels} 条`
    : (jobId ? `任务 #${jobId} 执行中…` : null)

  return (
    <Graph
      key={refreshKey}
      url="/api/v1/graph/companies"
      action={
        <>
          <button className="ghost" disabled={busy || !!jobId}
            onClick={() => submit('/api/v1/import-jobs',
              { provider: 'company-info', payload: {} })}>
            {jobId ? '导入中…' : '获取/导入公司'}
          </button>
          {statsText && <span className="note" style={{ marginLeft: 8 }}>{statsText}</span>}
          <ErrorBox error={error || jobError} />
        </>
      }
    />
  )
}
