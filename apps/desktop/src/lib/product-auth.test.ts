import { describe, expect, it } from 'vitest'

import { shouldRevalidateProductSession } from './product-auth'

describe('product auth error detection', () => {
  it.each([
    'Error code: 401 - API Key 已吊销',
    '{"type":"gateway_auth_failed"}',
    'API Key expired',
    '账户被禁用',
    '账号已停用，如有疑问请联系管理员'
  ])('revalidates on %s', message => {
    expect(shouldRevalidateProductSession(message)).toBe(true)
  })

  it('does not turn ordinary provider or connectivity errors into a logout', () => {
    expect(shouldRevalidateProductSession('HTTP 500 upstream unavailable')).toBe(false)
    expect(shouldRevalidateProductSession('Could not connect to Hermes gateway')).toBe(false)
  })
})
