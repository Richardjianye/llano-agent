import type { DirectoryAccount, DirectoryRow } from '@/api/directory'
import { describe, expect, it } from 'vitest'
import { directoryBatches, planDirectoryActions, uniqueCandidateBindings } from './directory-matching'

function account (uid: string): DirectoryAccount {
  return { uid, username: uid, name: uid, dept_id: null }
}
function row (key: string, candidates: string[], category = 'candidate'): DirectoryRow {
  return {
    key, category, candidates: candidates.map(uid => account(uid)), member: null, account: null,
    mapped_dept_ids: [], can_bind: true, can_create: false, can_disable: false,
  }
}

describe('reviewed candidate selection', () => {
  it('selects a unique eligible candidate without modifying accounts or choices', () => {
    const rows = [row('one', ['u1'])]
    const choices = { one: { action: null } }
    expect(uniqueCandidateBindings(rows, [account('u1')], choices, new Set())).toEqual([
      { key: 'one', action: 'bind', uid: 'u1' },
    ])
    expect(choices.one.action).toBeNull()
  })
  it('excludes ambiguous members, multiple members claiming one account, and ineligible accounts', () => {
    expect(uniqueCandidateBindings([
      row('one', ['u1']), row('two', ['u1']), row('ambiguous', ['u2', 'u3'], 'ambiguous'),
      row('inactive', ['u4']),
    ], [account('u1'), account('u2'), account('u3')], {}, new Set())).toEqual([])
  })
  it('preserves manual choices and excludes already completed or occupied targets', () => {
    expect(uniqueCandidateBindings([
      row('manual', ['u1']), row('completed', ['u2']), row('claimed', ['u3']),
    ], ['u1', 'u2', 'u3'].map(uid => account(uid)), {
      manual: { action: 'create', username: 'manual' }, other: { action: 'bind', uid: 'u3' },
    }, new Set(['completed']))).toEqual([])
  })
})

describe('partial directory submission', () => {
  it('submits complete selections while leaving blank and skipped rows untouched', () => {
    const rows = [row('ready', ['u1']), { ...row('blank', []), can_create: true }, row('skip', [])]
    const plan = planDirectoryActions(rows, [account('u1')], {
      ready: { action: 'bind', uid: 'u1' }, blank: { action: 'create', username: '' }, skip: { action: null },
    }, new Set())
    expect(plan.ready).toEqual([{ key: 'ready', action: 'bind', uid: 'u1' }])
    expect(plan.incomplete.blank).toContain('用户名')
    expect(plan.skipped).toBe(1)
  })
  it('keeps an inaccessible high-role target out of submission without blocking valid targets', () => {
    const plan = planDirectoryActions([row('high', ['president']), row('normal', ['u1'])], [account('u1')], {
      high: { action: 'bind', uid: 'president' }, normal: { action: 'bind', uid: 'u1' },
    }, new Set())
    expect(plan.ready.map(action => action.key)).toEqual(['normal'])
    expect(plan.incomplete.high).toContain('本人登录绑定')
  })
  it('submits 101 valid selections in 100 and 1 item batches', () => {
    const rows = Array.from({ length: 101 }, (_, i) => row('row-' + i, ['u-' + i]))
    const accounts = rows.map(r => r.candidates[0]!)
    const choices = Object.fromEntries(rows.map(r => [r.key, { action: 'bind' as const, uid: r.candidates[0]!.uid }]))
    const plan = planDirectoryActions(rows, accounts, choices, new Set())
    expect(plan.ready).toHaveLength(101)
    expect(directoryBatches(plan.ready).map(batch => batch.length)).toEqual([100, 1])
    expect(directoryBatches(plan.ready).flat().map(action => action.key)).toEqual(rows.map(r => r.key))
  })
  it('rejects duplicate local targets and ignores completed selections', () => {
    const plan = planDirectoryActions([row('one', ['u1']), row('two', ['u1']), row('done', ['u2'])], [account('u1'), account('u2')], {
      one: { action: 'bind', uid: 'u1' }, two: { action: 'bind', uid: 'u1' }, done: { action: 'bind', uid: 'u2' },
    }, new Set(['done']))
    expect(plan.ready).toEqual([])
    expect(Object.keys(plan.incomplete)).toEqual(['one', 'two'])
  })
})
