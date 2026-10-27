import type { MemberHit } from '@/api/members'
import { describe, expect, it } from 'vitest'
import { memberLabel, mergeMemberOptions, missingMemberUids } from './member-picker'

function hit (uid: string, name = uid): MemberHit {
  return {
    uid, username: `acc-${uid}`, realname: name, nickname: null, display_name: name,
    role: 'employee', status: 1, dept_id: 30, dept_name: '设计创意部', dept_path: '视觉中心 / 设计创意部',
  }
}

describe('member picker options', () => {
  it('keeps selected members visible when a new search no longer returns them', () => {
    const selected = [hit('u260', '源贺')]
    const options = mergeMemberOptions(selected, [hit('u001'), hit('u260', '源贺')])
    expect(options.map(m => m.uid)).toEqual(['u260', 'u001'])
  })

  it('looks up only the selected uids it cannot label yet', () => {
    expect(missingMemberUids(['u1', 'u2', 'u2', ''], [hit('u1')])).toEqual(['u2'])
  })

  it('shows the account next to the name only when they differ', () => {
    expect(memberLabel(hit('u1', '源贺'))).toBe('源贺（acc-u1）')
    expect(memberLabel({ display_name: 'same', username: 'same', dept_path: '' })).toBe('same')
  })
})
