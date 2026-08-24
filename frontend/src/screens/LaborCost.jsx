import { useMemo, useState } from 'react'
import { useFetch, useDataOp } from './useDataOp'
import { ErrorBox } from './ui'

/**
 * 加薪规则 / 用工成本屏（API② · F-P1-10）：
 * - 规则区：加薪规则可视化（Level→调整%、外包系数、晋级、每年 CPI 增幅）——税率公式细节不显示。
 * - 计算区：年份输入 + 「拉取岗位并计算」→ POST /labor-cost/compute，成功 refetch 结果。
 * - 结果区：每公司×年用工成本只读表。
 */
export default function LaborCost() {
  const [year, setYear] = useState('2001')
  const rules = useFetch('/api/v1/labor-cost/rules')
  const results = useFetch('/api/v1/labor-cost/results' + (year ? `?year=${year}` : ''))
  const op = useDataOp(() => results.refresh())

  const doCompute = () => op.submit('/api/v1/labor-cost/compute', { year: Number(year) || 2001 })

  const r = rules.data || {}
  const levelEntries = useMemo(() => Object.entries(r.level_adjust_pct || {}), [r])
  const outsourceEntries = Object.entries(r.outsource_factor || {})
  const items = results.data?.items || []

  return (
    <div className="screen">
      <div className="panel">
        <h3>用工成本 · 数据</h3>
        <p className="note">来源：人才成本基准（工资/CPI/税率）+ 外部在岗岗位（API②）</p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
          <label className="note">年份</label>
          <input type="number" min={1900} max={2999} value={year}
            onChange={e => setYear(e.target.value)} />
          <button className="primary" disabled={op.busy} onClick={doCompute}>
            {op.busy ? '计算中…' : '拉取岗位并计算'}
          </button>
          <ErrorBox error={op.error} />
        </div>
        {op.last && (
          <p className="note">
            已处理 {op.last.positions_fetched ?? 0} 个岗位 → {op.last.companies_computed?.companies?.length ?? 0} 家公司落账。
          </p>
        )}
      </div>

      <div className="panel">
        <h3>加薪规则</h3>
        <p className="note">基础年薪 = 工作地点地区「投资/金融行业年薪」（按岗位 opening 年份）× (1 + Level 调整%)</p>
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead>
            <tr><th>级别</th><th>调整%</th></tr>
          </thead>
          <tbody>
            {levelEntries.map(([k, v]) => (
              <tr key={k}><td>{k}</td><td>{v}%</td></tr>
            ))}
          </tbody>
        </table>
        <p className="note">{r.cpi_rule}</p>
        <p className="note">{r.base_rule ? '基础：' + r.base_rule : ''}</p>
      </div>

      <div className="panel">
        <h3>外包类基准</h3>
        <p className="note">{r.outsource_base}</p>
        {outsourceEntries.map(([k, v]) => (
          <p key={k} className="note">{k}：× {v}</p>
        ))}
        <p className="note">晋升：每跨一级 × (1 + {r.promotion_step_pct}%)；固定奖金：默认 {r.bonus_months_default} 个月，日本 {r.bonus_months_japan} 个月</p>
      </div>

      <div className="panel">
        <h3>用工成本结果{year ? `（${year}）` : ''}</h3>
        {items.length === 0 ? (
          <p className="note">{op.busy ? '…' : '暂无结果（先点「拉取岗位并计算」）'}</p>
        ) : (
          <table style={{ borderCollapse: 'collapse', width: '100%' }}>
            <thead>
              <tr><th>年份</th><th>公司</th><th>币种</th><th>用工成本</th></tr>
            </thead>
            <tbody>
              {items.map((x, i) => (
                <tr key={i}>
                  <td>{x.year}</td><td>{x.company_name}</td>
                  <td>{x.currency}</td>
                  <td>{x.amount != null ? x.amount.toLocaleString() : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}