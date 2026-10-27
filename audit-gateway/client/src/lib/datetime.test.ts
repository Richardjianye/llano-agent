import { describe, expect, it } from 'vitest'

import { formatDateTime } from './datetime'

describe('formatDateTime', () => {
  it('drops the T from the server-local (already +08:00) ISO strings the gateway returns', () => {
    expect(formatDateTime('2026-09-23T17:46:09')).toBe('2026-09-23 17:46:09')
    expect(formatDateTime('2026-09-23T17:46:09.123456')).toBe('2026-09-23 17:46:09')
  })

  it('converts UTC release timestamps to Beijing time (+8), not just drops the T', () => {
    expect(formatDateTime('2026-09-20T05:03:52Z')).toBe('2026-09-20 13:03:52')
    expect(formatDateTime('2026-09-20T20:00:00Z')).toBe('2026-09-21 04:00:00')
    expect(formatDateTime('2026-09-20T13:03:52+08:00')).toBe('2026-09-20 13:03:52')
  })

  it('shows a dash for missing values', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('')).toBe('—')
  })
})
