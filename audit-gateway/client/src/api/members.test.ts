import { describe, expect, it } from 'vitest'
import { memberSearchParams } from './members'

describe('member search params', () => {
  it('sends only what was asked, with a default page size', () => {
    expect(memberSearchParams({})).toEqual({ limit: 20 })
    expect(memberSearchParams({ keyword: '  源贺 ' })).toEqual({ keyword: '源贺', limit: 20 })
  })

  it('joins arrays and includes descendants by default when filtering departments', () => {
    expect(memberSearchParams({ deptIds: [9, 30], roles: ['manager'], uids: ['a', 'a', 'b'], limit: 50, offset: 100 }))
      .toEqual({ dept_ids: '9,30', include_descendants: true, roles: 'manager', uids: 'a,b', limit: 50, offset: 100 })
    expect(memberSearchParams({ deptIds: [9], includeDescendants: false }).include_descendants).toBe(false)
  })

  it('only sends status when it differs from the server default', () => {
    expect(memberSearchParams({ status: 'active' }).status).toBeUndefined()
    expect(memberSearchParams({ status: 'all' }).status).toBe('all')
  })
})
