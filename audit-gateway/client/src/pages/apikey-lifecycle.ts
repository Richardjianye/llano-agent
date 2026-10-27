/** API Key 状态与吊销原因的展示（2026-10-19） */

import type { ApiKeyRecord, ApiKeyState } from '@/types/api'

export const KEY_STATE_META: Record<ApiKeyState, { label: string, color: string }> = {
  active: { label: '有效', color: 'success' },
  idle: { label: '闲置失效', color: 'warning' },
  expired: { label: '已过期', color: 'grey' },
  revoked: { label: '已吊销', color: 'grey' },
}

export const REVOKE_REASON_LABELS: Record<string, string> = {
  client_logout: '客户端退出登录',
  admin: '管理员吊销',
  admin_batch: '管理员批量吊销',
  account_disabled: '账号停用',
  password_reset: '管理员重置密码',
  self_password_change: '本人改密时吊销',
}

/** 老接口不带 state 时按 status 兜底 —— 只分得出有效/吊销 */
export function keyState (record: Pick<ApiKeyRecord, 'state' | 'status'>): ApiKeyState {
  return record.state ?? (record.status === 1 ? 'active' : 'revoked')
}

export function revokeReasonText (reason: string | null | undefined): string {
  if (!reason) {
    return '原因未记录'
  }
  const idle = /^idle_(\d+)d$/.exec(reason)
  if (idle) {
    return `闲置超过 ${idle[1]} 天自动吊销`
  }
  return REVOKE_REASON_LABELS[reason] ?? reason
}

/** 「12 天后闲置失效」；已经不在有效期内、或不参与闲置判定时为空 */
export function idleHint (record: Pick<ApiKeyRecord, 'idle_expires_at' | 'state' | 'status'>, now: Date): string {
  if (keyState(record) !== 'active' || !record.idle_expires_at) {
    return ''
  }
  const ms = new Date(record.idle_expires_at).getTime() - now.getTime()
  const days = Math.max(0, Math.ceil(ms / 86_400_000))
  return days <= 0 ? '即将闲置失效' : `${days} 天未使用将失效`
}
