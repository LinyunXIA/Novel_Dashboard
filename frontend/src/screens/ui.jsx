/** 共享小 UI 原语（F-P1-09 操作模板共用）。 */
export function Field({ label, children }) {
  return (
    <label className="field">
      <span className="flabel">{label}</span>
      {children}
    </label>
  )
}

export function ErrorBox({ error }) {
  if (!error) return null
  const text = typeof error === 'string' ? error : JSON.stringify(error)
  return <div className="errbox">{text}</div>
}

export function fmt(n) {
  // 八轮审计 #190：非数值输入守卫（此前字符串会输出 "NaN" 字样）
  if (typeof n !== 'number' || Number.isNaN(n)) return n === null || n === undefined ? '—' : String(n)
  const abs = Math.abs(n)
  if (abs >= 1e8) return (n / 1e8).toFixed(2) + ' 亿'
  if (abs >= 1e4) return (n / 1e4).toFixed(1) + ' 万'
  return Number(n).toLocaleString()
}