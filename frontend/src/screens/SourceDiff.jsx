import { useState } from 'react'
import { useFetch, useDataOp } from './useDataOp'
import { ErrorBox } from './ui'

const STATUS_BADGE = { new: '新增', unchanged: '一致', changed: '待决策' }

/**
 * 版本 / diff 决策屏（F-P2-06 · §11）：文件变更 → 看 diff → 采纳新版本 / 回退。
 */
export default function SourceDiff() {
  const [refresh, setRefresh] = useState(0)
  const files = useFetch(`/api/v1/source-files?refresh=${refresh}`)
  const [selVid, setSelVid] = useState(null)     // 当前选中文件（用 current_version id 标识）
  const [diffVersion, setDiffVersion] = useState('')
  // issue #143：查询串拼接统一用 &（此前 diffVersion 与 refresh 各带一个 ?，产生双问号）
  const diffQs = diffVersion ? `?version_id=${diffVersion}&refresh=${refresh}` : `?refresh=${refresh}`
  const diff = useFetch(selVid ? `/api/v1/source-files/${selVid}/diff${diffQs}` : null)
  const versions = useFetch(selVid ? `/api/v1/source-files/${selVid}/versions?refresh=${refresh}` : null)
  const op = useDataOp(() => { setRefresh(r => r + 1); diff.refresh() })

  const items = files.data?.items || []
  const diffData = diff.data

  const selectFile = (f) => {
    setSelVid(f.current_version)
    setDiffVersion('')
  }
  const adopt = (f) => op.submit(`/api/v1/source-files/${f.current_version}/versions`, {})
  // issue #139：回退被 409 拒绝（磁盘已偏离当前版）时同样刷新 diff，引导重新决策
  const restore = async (f, vid) => {
    const res = await op.submit(`/api/v1/source-files/${f.current_version}/versions/${vid}/restore`, {})
    if (!res.ok) { setRefresh(r => r + 1); diff.refresh() }
  }

  const renderDiff = (s) => {
    if (!s) return <p className="note">选择文件查看 diff</p>
    if (!s.diff_str) return <p className="note">无差异</p>
    return <pre className="mono" style={{ maxHeight: 320, overflow: 'auto', background: '#fff' }}>
      {s.diff_str.split('\n').map((ln, i) => {
        let cls = ''
        if (ln.startsWith('+') && !ln.startsWith('+++')) cls = 'diff-add'
        else if (ln.startsWith('-') && !ln.startsWith('---')) cls = 'diff-del'
        return <div key={i} className={cls || undefined}>{ln}</div>
      })}
    </pre>
  }

  return (
    <div className="screen">
      <div className="panel">
        <h3>版本 / diff 决策</h3>
        <p className="note">F-P2-06 · §11：文件变更检测 + 采纳新版本 / 回退（复原源文件 + 更新版本）</p>
        <ErrorBox error={op.error} />

        <h4>被跟踪文件（{items.length}）</h4>
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th>文件</th><th>状态</th><th>当前版本</th><th></th></tr></thead>
          <tbody>
            {items.map(f => (
              <tr key={f.file}>
                <td>{f.file}</td>
                <td>
                  <span className={`badge ${f.status === 'changed' ? 'warn' : ''}`}>
                    {STATUS_BADGE[f.status] || f.status}</span>
                </td>
                <td className="mono">v{f.current_version}</td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <button className="ghost" onClick={() => selectFile(f)}>看 diff / 版本</button>
                  {f.status === 'changed' && (
                    <button className="primary" disabled={op.busy} onClick={() => adopt(f)}>采纳新版本</button>
                  )}
                  {(f.current_version && f.current_version > 1) && (
                    <button className="ghost" disabled={op.busy} onClick={() => restore(f, f.current_version - 1)}>回退上一版</button>
                  )}
                </td>
              </tr>
            ))}
            {!items.length && <tr><td colSpan="4" className="note">暂无被跟踪文件（导入后出现）</td></tr>}
          </tbody>
        </table>

        {selVid && (
          <>
            <h4 style={{ marginTop: 16 }}>diff：当前 vs 磁盘 / 历史版本</h4>
            <div style={{ marginBottom: 8 }}>
              <select value={diffVersion} onChange={e => setDiffVersion(e.target.value)}>
                <option value="">磁盘当前 vs is_current</option>
                {(versions.data?.versions || []).map(v => (
                  <option key={v.id} value={v.id}>v{v.version} ({v.captured_at})</option>
                ))}
              </select>
            </div>
            {diff.err ? <ErrorBox error={String(diff.err)} /> : renderDiff(diffData)}
          </>
        )}
      </div>
    </div>
  )
}