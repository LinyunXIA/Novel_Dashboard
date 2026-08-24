import { useEffect, useState } from 'react'
import Dashboard from './screens/Dashboard'
import Invest from './screens/Invest'
import Transfer from './screens/Transfer'
import Returns from './screens/Returns'
import Finance from './screens/Finance'
import Graph from './screens/Graph'

const TABS = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'invest', label: '投资' },
  { key: 'transfer', label: '划拨/换汇' },
  { key: 'returns', label: '收益曲线' },
  { key: 'finance', label: '财务收支' },
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
        {active === 'persons' && <Graph url="/api/v1/graph/persons" />}
        {active === 'companies' && <Graph url="/api/v1/graph/companies" />}
        {active === 'graphall' && <Graph url="/api/v1/graph/all" />}
        {(active === 'timeline' || active === 'search' || active === 'health') && (
          <Placeholder label={TABS.find(t => t.key === active).label} asOf={asOf} />
        )}
      </main>
    </div>
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