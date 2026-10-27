/**
 * 运维探针 —— /healthz 与 /readyz 返回的是裸 JSON，不走统一响应信封，
 * 所以这里不能用 http.ts 里的 request()
 */

export interface HealthInfo {
  status: string
  uptime_seconds: number
}

export interface ReadinessInfo {
  status: string
  checks: Record<string, string>
}

export async function fetchHealth (): Promise<HealthInfo> {
  const res = await fetch('/healthz', { credentials: 'include' })
  if (!res.ok) {
    throw new Error(`网关无响应（HTTP ${res.status}）`)
  }
  return (await res.json()) as HealthInfo
}

/** 依赖不通时后端返回 503，此时响应体仍是合法 JSON，照常解析 */
export async function fetchReadiness (): Promise<ReadinessInfo> {
  const res = await fetch('/readyz', { credentials: 'include' })
  return (await res.json()) as ReadinessInfo
}
