import { afterEach, expect, it, vi } from 'vitest'

const count = vi.hoisted(() => vi.fn())

vi.mock('@/hermes', () => ({ getCompanySkillUpdateCount: () => count() }))

import { $companySkillUpdates, refreshCompanySkillUpdates, setCompanySkillUpdates } from './skill-updates'

afterEach(() => {
  count.mockReset()
  setCompanySkillUpdates(0)
})

it('keeps the last known count when a background check fails', async () => {
  count.mockResolvedValueOnce({ count: 2 })
  await refreshCompanySkillUpdates()
  expect($companySkillUpdates.get()).toBe(2)

  count.mockRejectedValueOnce(new Error('offline'))
  await refreshCompanySkillUpdates()
  expect($companySkillUpdates.get()).toBe(2)

  setCompanySkillUpdates(-1)
  expect($companySkillUpdates.get()).toBe(0)
})
