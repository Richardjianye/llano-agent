// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { SkillInfo } from '@/hermes'

import { CompanyUpdateAll, companyUpdateLabel, CompanyUpdatePanel } from './company-update'

const api = vi.hoisted(() => ({ keep: vi.fn(), update: vi.fn(), updateAll: vi.fn() }))
const notify = vi.hoisted(() => vi.fn())
const notifyError = vi.hoisted(() => vi.fn())

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<object>()),
  keepCompanySkillVersion: (...args: unknown[]) => api.keep(...args),
  updateAllCompanySkills: () => api.updateAll(),
  updateCompanySkill: (...args: unknown[]) => api.update(...args)
}))
vi.mock('@/store/notifications', () => ({
  notify: (input: unknown) => notify(input),
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const skill = (state: Partial<NonNullable<SkillInfo['hub_update']>>): SkillInfo => ({
  name: 'weekly-report',
  category: 'productivity',
  display_name: '周报',
  description: '汇总一周销售',
  enabled: true,
  provenance: 'hub',
  hub_update: {
    delisted: false,
    update_available: true,
    locally_modified: false,
    installed_version: 1,
    latest_version: 2,
    latest_bundle_hash: 'h2',
    changelog: '补充核对步骤',
    ...state
  }
})

function view(node: React.ReactNode) {
  render(<QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>)
}

it('labels rows with the new version or the delisting', () => {
  const u = { badge: (v: number | string) => `v${v} available`, delisted: 'Delisted' }

  expect(companyUpdateLabel(skill({}), u)).toBe('v2 available')
  expect(companyUpdateLabel(skill({ delisted: true, update_available: false }), u)).toBe('Delisted')
  expect(companyUpdateLabel(skill({ update_available: false }), u)).toBeNull()
})

it('updates an untouched skill in one click and says it applies to new chats', async () => {
  api.update.mockResolvedValue({ name: 'weekly-report', updated: true, version: 2, backup: null })
  view(<CompanyUpdatePanel skill={skill({})} />)

  expect(screen.getByText('What changed: 补充核对步骤')).toBeTruthy()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Update' })))

  expect(api.update).toHaveBeenCalledWith('weekly-report', false)
  expect(notify).toHaveBeenCalledWith(expect.objectContaining({ title: 'Updated “周报” to v2' }))
})

it('never offers a plain overwrite when the skill was changed locally', async () => {
  api.update.mockResolvedValue({ name: 'weekly-report', updated: true, version: 2, backup: '/skills/.archive/x' })
  api.keep.mockResolvedValue({ ok: true })
  view(<CompanyUpdatePanel skill={skill({ locally_modified: true })} />)

  expect(screen.queryByRole('button', { name: 'Update' })).toBeNull()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Back up my changes and update' })))
  expect(api.update).toHaveBeenCalledWith('weekly-report', true)
  expect(notify).toHaveBeenCalledWith(
    expect.objectContaining({ message: expect.stringContaining('/skills/.archive/x') })
  )

  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Keep my version' })))
  expect(api.keep).toHaveBeenCalledWith('weekly-report', 'h2')
})

it('update-all reports skipped local changes instead of hiding them', async () => {
  api.updateAll.mockResolvedValue({
    updated: [{ name: 'a', updated: true, version: 3, backup: null }],
    locally_modified: ['weekly-report'],
    failed: []
  })
  view(<CompanyUpdateAll count={2} />)

  expect(screen.getByText('2 company skills have a new version')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Update all' }))

  await waitFor(() =>
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'warning',
        title: 'Updated 1; 1 changed on this computer were not updated',
        message: expect.stringContaining('weekly-report')
      })
    )
  )
})

it('shows nothing when there is nothing to update', () => {
  view(<CompanyUpdateAll count={0} />)
  expect(screen.queryByRole('button')).toBeNull()
})
