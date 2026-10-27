import { describe, expect, it } from 'vitest'
import { idleHint, keyState, revokeReasonText } from './apikey-lifecycle'

describe('api key lifecycle display', () => {
  it('falls back to status when an older server sends no state', () => {
    expect(keyState({ status: 1 })).toBe('active')
    expect(keyState({ status: 0 })).toBe('revoked')
    expect(keyState({ status: 1, state: 'idle' })).toBe('idle')
  })

  it('counts down to idle expiry only for keys that are still active', () => {
    const now = new Date('2026-10-19T00:00:00')
    expect(idleHint({ status: 1, state: 'active', idle_expires_at: '2026-10-31T00:00:00' }, now)).toBe('12 天未使用将失效')
    expect(idleHint({ status: 1, state: 'active', idle_expires_at: '2026-10-18T00:00:00' }, now)).toBe('即将闲置失效')
    expect(idleHint({ status: 0, state: 'revoked', idle_expires_at: '2026-10-31T00:00:00' }, now)).toBe('')
    expect(idleHint({ status: 1, state: 'active', idle_expires_at: null }, now)).toBe('')
  })

  it('labels known revoke reasons and admits unknown ones', () => {
    expect(revokeReasonText('client_logout')).toBe('客户端退出登录')
    expect(revokeReasonText('idle_30d')).toBe('闲置超过 30 天自动吊销')
    expect(revokeReasonText(null)).toBe('原因未记录')
    expect(revokeReasonText('future_reason')).toBe('future_reason')
  })
})
