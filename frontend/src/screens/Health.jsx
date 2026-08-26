import { useFetch } from './useDataOp'
import { ErrorBox } from './ui'

const RULES = ['H1', 'H2', 'H3', 'H4', 'H5', 'H-STOCK']

/** F-U4 健康校验屏（issue #123）：H1–H5/H-STOCK 汇总 + 问题清单（文件/行/规则/明细）。 */
export default function Health() {
  const { data, err } = useFetch('/api/v1/health')
  const summary = data?.summary || {}
  const findings = data?.findings || []

  return (
    <div className="cols2">
      <ErrorBox error={err} />
      <div className="panel">
        <h3>健康校验汇总（H1–H5 · H-STOCK）</h3>
        <p className="note">GET /api/v1/health · 导入/UI 改动后重算完成时会自动复核（摘要随通知推送）</p>
        <table>
          <thead><tr><th>规则</th><th className="num">问题数</th><th>warn / crit</th><th>状态</th></tr></thead>
          <tbody>
            {RULES.map(r => {
              const x = summary[r] || { total: 0, warn: 0, crit: 0 }
              return (
                <tr key={r}>
                  <td>{r}</td>
                  <td className="num">{x.total}</td>
                  <td className="num muted">{x.warn} / {x.crit}</td>
                  <td>{x.total ? '⚠ 需关注' : '✓'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>问题清单（{findings.length}）</h3>
        <table>
          <thead><tr><th>级别</th><th>规则</th><th>位置</th><th>明细</th></tr></thead>
          <tbody>
            {findings.map((f, i) => (
              <tr key={i}>
                <td>{f.level === 'crit' ? '🔴' : '🟡'} {f.level}</td>
                <td>{f.rule}</td>
                <td className="mono" style={{ fontSize: 12 }}>{f.location}</td>
                <td style={{ fontSize: 12 }}>{f.detail}</td>
              </tr>
            ))}
            {!findings.length && <tr><td colSpan={4} className="note">未发现问题 ✓</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
