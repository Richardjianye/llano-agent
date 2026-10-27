// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { HubDiscovery } from './hub-discovery'

const catalog = vi.hoisted(() => vi.fn())
vi.mock('@/hermes', () => ({ getSkillHubCatalog: (...args: unknown[]) => catalog(...args) }))
vi.mock('./hub-row', () => ({ HubSkillRow: ({ skill }: { skill: { name: string } }) => <div>{skill.name}</div> }))
vi.mock('@/store/profile', async () => ({ $activeGatewayProfile: (await import('nanostores')).atom(null) }))
vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      skills: {
        hub: {
          companyLibrary: 'Company',
          recommended: 'Recommended',
          packages: 'Packages',
          emptyDepartment: 'No published skills',
          currentDepartment: 'Your department',
          catalogFailed: 'Could not load',
          retry: 'Retry',
          searching: 'Loading',
          loadMore: 'More'
        }
      }
    }
  })
}))
afterEach(() => {
  cleanup()
  catalog.mockReset()
})

it('shows empty departments and global totals while the public catalog fails', async () => {
  const departments = [
    { id: 1, name: 'Own empty department', count: 0, is_current: true, skills: [], has_more: false, next_offset: 0 },
    { id: 2, name: 'Finance', count: 105, is_current: false, skills: [], has_more: true, next_offset: 0 }
  ]

  catalog.mockImplementation((section: string, offset: number, id?: number) =>
    section === 'company'
      ? Promise.resolve({
          departments:
            id === 2
              ? [{ ...departments[1], skills: [{ name: 'Report', identifier: 'llano-hub/report' }] }]
              : departments
        })
      : Promise.reject(new Error('Offline'))
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <HubDiscovery installed={{}} onPreview={() => {}} />
    </QueryClientProvider>
  )
  await waitFor(() => expect(screen.getByText('Own empty department')).toBeTruthy())
  expect(screen.getByText('(105)')).toBeTruthy()
  expect(screen.queryByText('Report')).toBeNull()
  expect(screen.getByRole('tab', { name: /Own empty department/ }).getAttribute('aria-selected')).toBe('true')
  expect(screen.getByText('No published skills')).toBeTruthy()
  await waitFor(() => expect(screen.getAllByText('Could not load')).toHaveLength(2))
  expect(catalog).toHaveBeenCalledWith('recommended', 0)
  expect(catalog).toHaveBeenCalledWith('packages', 0)
  fireEvent.mouseDown(screen.getByRole('tab', { name: /Finance/ }), { button: 0, ctrlKey: false })
  await waitFor(() => expect(screen.getByText('Report')).toBeTruthy())
  expect(screen.queryByText('No published skills')).toBeNull()
  expect(catalog).toHaveBeenCalledWith('company', 0, 2)
  client.clear()
})
