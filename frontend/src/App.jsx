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

      {/* issue #122：重算完成非阻断横幅（§9.3 / F-U3） */}
      <NotificationsBanner />

      <nav className="nav">
        {TABS.map(t => (
          <button key={t.key} className={`tab ${active === t.key ? 'active' : ''}`}
            onClick={() => setActive(t.key)}>{t.label}</button>
        ))}
      </nav>

      <main className="screen">
        {active === 'dashboard' && <Dashboard asOf={asOf} />}
        {/* issue #121：数据屏以 asOf 作 key——游标变动即重挂载重新拉取，全 App 联动 */}
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
        {active === 'movies' && <Movies />}
        {active === 'stock' && <Stock />}
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
 * 重算通知横幅（issue #122 · DESIGN §9.3 / PRD F-U3）：
 * 轮询 GET /notifications（默认仅未读）→ recompute-done 弹非阻断横幅，
 * 附 payload.health 摘要（issue #120）；「知道了」PATCH 标记已读。
 */
function NotificationsBanner() {
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
      {items.map(n => (
        <div key={n.id} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span>🔔 {n.message}{healthText(n.payload?.health) && ` · ${healthText(n.payload.health)}`}</span>
          <button className="ghost" onClick={() => ack(n.id)}>知道了</button>
        </div>
      ))}
    </div>
  )
}

/**
 * 公司图谱屏（F-P1-05 / F-U7）：公司图谱 + 右上「获取/导入公司」按钮，人工触发
 * POST /api/v1/graph/companies/import（外部系统 API① 公司基础信息）→ 成功后 bump key
 * 重挂载 Graph 重新拉取展示新节点/边（后端已在同一请求内 commit）。
 */
function CompanyGraph() {
  const [refreshKey, setRefreshKey] = useState(0)
  const [stats, setStats] = useState(null)
  const { submit, busy, error } = useDataOp(data => {
    setStats(data?.stats || null)
    setRefreshKey(k => k + 1)
  })

  const statsText = stats
    ? `已处理 ${stats.companies} 家公司 · 新增 ${stats.companies_created} 家 · 股权关系 ${stats.rels} 条`
    : null

  return (
    <Graph
      key={refreshKey}
      url="/api/v1/graph/companies"
      action={
        <>
          <button className="ghost" disabled={busy}
            onClick={() => submit('/api/v1/graph/companies/import', {})}>
            {busy ? '导入中…' : '获取/导入公司'}
          </button>
          {statsText && <span className="note" style={{ marginLeft: 8 }}>{statsText}</span>}
          <ErrorBox error={error} />
        </>
      }
    />
  )
}

function Placeholder({ label, asOf }) {
  return (
    <div className="panel">
      <h3>{label}</h3>
      <p className="note">「{label}」屏待后续：截至 {asOf}。</p>
    </div>
  )
}