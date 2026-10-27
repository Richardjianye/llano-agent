import { describe, expect, it } from 'vitest'
import { credentialsCsv } from './directory'

describe('initial account CSV', () => {
  it('keeps Chinese names and quoted cells while blocking spreadsheet formulas', () => {
    const csv = credentialsCsv([{ username: '=HYPERLINK("x")', initial_password: '+random-secret', name: '张三', uid: 'uid' }])
    expect(csv.startsWith('\uFEFF')).toBe(true)
    expect(csv).toContain('张三')
    expect(csv).toContain('"\'=HYPERLINK(""x"")"')
    expect(csv).toContain('"\'+random-secret"')
  })
})
