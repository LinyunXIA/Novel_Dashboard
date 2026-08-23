import { useEffect, useState } from 'react'
import Dashboard from './screens/Dashboard'

const TABS = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'search', label: '搜索' },
  { key: 'invest', label: '投资' },
  { key: 'returns', label: '收益曲线' },
  { key: 'timeline', label: '编年史' },
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
          <span className="env">F-P0 · 前端骨架 · {health === null ? 'API 未连接' : 'API 已连接'}</span>
        </div>
      </header>

      {/* 全局日历游标（全 App 生效） */}
      <div className="panel calendar-bar">
        <span className="label">全局日历游标 · 截至日期（全 App 生效）</span>
        <input
          type="date"
          value={asOf}
          min="1947-01-01"
          max="2026-12-31"
          onChange={e => setAsOf(e.target.value)}
        />
        <span className="mono">{asOf}</span>
        <span className="note">拖动后：所有屏统一显示「截至该日」状态</span>
      </div>

      <nav className="nav">
        {TABS.map(t => (
          <button
            key={t.key}
            className={`tab ${active === t.key ? 'active' : ''}`}
            onClick={() => setActive(t.key)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <main className="screen">
        {active === 'dashboard' && <Dashboard asOf={asOf} />}
        {(active === 'search' || active === 'invest' || active === 'returns' ||
          active === 'timeline' || active === 'health') && (
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
      <p className="note">「{label}」屏为前端骨架占位；截至 {asOf}。后续里程碑填充真实交互（投资/搜索/曲线/编年史等）。</p>
    </div>
  )
}