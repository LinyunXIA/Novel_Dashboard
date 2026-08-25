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
  { key: 'search', label: '搜索' },
  { key: 'health', label: '健康校验' },
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

      <nav className="nav">
        {TABS.map(t => (
          <button key={t.key} className={`tab ${active === t.key ? 'active' : ''}`}
            onClick={() => setActive(t.key)}>{t.label}</button>
        ))}
      </nav>

      <main className="screen">
        {active === 'dashboard' && <Dashboard asOf={asOf} />}
        {active === 'invest' && <Invest />}
        {active === 'transfer' && <Transfer />}
        {active === 'returns' && <Returns />}
        {active === 'finance' && <Finance />}
        {active === 'labor' && <LaborCost />}
        {active === 'persons' && <Graph url="/api/v1/graph/persons" />}
        {active === 'companies' && <CompanyGraph />}
        {active === 'graphall' && <Graph url="/api/v1/graph/all" />}
        {active === 'movies' && <Movies />}
        {active === 'stock' && <Stock />}
        {active === 'search' && <Search />}
        {(active === 'timeline' || active === 'health') && (
          <Placeholder label={TABS.find(t => t.key === active).label} asOf={asOf} />
        )}
      </main>
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
      <p className="note">「{label}」屏待后续：截至 {asOf}。搜索(F-P1-08)阻塞于本地 omlx 不可用。</p>
    </div>
  )
}