/**
 * Product gateway authentication failures arrive inside Hermes' turn error
 * text, after crossing the Python sidecar. Revalidate only on auth-shaped
 * errors; the authoritative decision still happens in Electron against Audit
 * Gateway, so a coincidental provider error can never clear the local session.
 */
export function shouldRevalidateProductSession(message: string): boolean {
  return (
    /\b401\b/.test(message) ||
    /gateway_auth_failed/i.test(message) ||
    /api key (?:invalid|revoked|expired)/i.test(message) ||
    /api key (?:无效|已吊销|已过期)/i.test(message) ||
    /账户被禁用|账号已被禁用|账号已停用/.test(message)
  )
}
