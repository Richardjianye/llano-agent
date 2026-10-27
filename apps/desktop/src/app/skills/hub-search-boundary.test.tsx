// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'

const search = vi.fn()
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getSkillHubSources: async () => ({
    sources: [
      { id: 'llano-hub', label: '公司技能库', searchable: true },
      { id: 'skillhub', label: '公开技能库', searchable: true },
      { id: 'local', label: '本机技能', searchable: true }
    ],
    installed: {},
    featured: [],
    company: []
  }),
  searchSkillsHub: (...args: unknown[]) => search(...args)
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('never searches a private source or renders private rows, while keeping installed catalog results', async () => {
  const row = {
    name: '公司报表',
    description: '汇总公司销售金额',
    source: 'skillhub',
    identifier: 'skillhub/report',
    trust_level: 'community',
    tags: []
  }

  search.mockResolvedValue({
    results: [row, { ...row, name: '本机秘密', source: 'local', identifier: 'local/private' }],
    installed: { 'skillhub/report': { name: 'report' } },
    source_counts: {},
    timed_out: []
  })
  const { SkillsHub } = await import('./hub')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SkillsHub query="报表" />
      </MemoryRouter>
    </QueryClientProvider>
  )
  fireEvent.click(await screen.findByRole('button', { name: 'Search public sources' }))
  await waitFor(() => expect(search).toHaveBeenCalledWith('报表', 'skillhub'))
  expect(search.mock.calls.some(([, source]) => source === 'local')).toBe(false)
  expect(await screen.findByText('公司报表')).toBeTruthy()
  expect(screen.queryByText('本机秘密')).toBeNull()
  expect(screen.getByRole('button', { name: 'Uninstall' })).toBeTruthy()
  client.clear()
})
