import { useEffect, useState } from 'react'

/**
 * F-P1-09 统一改数据操作模板（DESIGN §6.8/§19）：
 * useDataOp —— 提交一个 UI 派生写操作 → 失败时「整体拒绝」并展示服务端 detail
 *   （409/422 消息，表单保留不回滚）→ 成功后回调 onSuccess（各屏触发 refetch）。
 *   后传重算由后端在**同一请求内同步完成**（create_investment/transfer 后调用
 *   recompute + 快照 + recompute-done notification），前端无需另发 recompute POST。
 * useFetch —— 轻量数据拉取：url/deps 变化或 refresh() 时重取。
 */
export function useDataOp(onSuccess) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [last, setLast] = useState(null)

  const submit = async (url, body) => {
    setBusy(true)
    setError(null)
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await r.json().catch(() => null)
      if (!r.ok) {
        const detail = data?.detail
        const msg = typeof detail === 'string' ? detail
          : (detail ? JSON.stringify(detail) : `HTTP ${r.status}`)
        setError(msg)
        return { ok: false, data }
      }
      setLast(data)
      if (onSuccess) onSuccess(data)
      return { ok: true, data }
    } catch (e) {
      setError(String(e))
      return { ok: false }
    } finally {
      setBusy(false)
    }
  }
  return { submit, busy, error, last }
}

export function useFetch(url) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [tick, setTick] = useState(0)
  const refresh = () => setTick(t => t + 1)
  useEffect(() => {
    let alive = true
    if (!url) { setData(null); return () => { alive = false } }
    setErr(null)
    fetch(url).then(r => r.json()).then(
      d => { if (alive) setData(d) },
      e => { if (alive) setErr(String(e)) },
    )
    return () => { alive = false }
  }, [url, tick])
  return { data, err, refresh }
}