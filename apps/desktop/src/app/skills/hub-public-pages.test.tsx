// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { usePublicCatalog } from './hub-public-pages'

const catalog = vi.hoisted(() => vi.fn())
vi.mock('@/hermes', () => ({ getSkillHubCatalog: (...args: unknown[]) => catalog(...args) }))
afterEach(() => {
  cleanup()
  localStorage.clear()
  catalog.mockReset()
  vi.restoreAllMocks()
})

it.each(['recommended', 'packages'] as const)(
  'caches %s totals, randomizes later visits and loads the following page',
  async section => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )

    catalog.mockImplementation(async (_section, offset) => ({
      total: 30,
      skills: [{ name: String(offset) }],
      packages: [{ name: String(offset) }]
    }))
    vi.spyOn(Math, 'random').mockReturnValue(0.5)
    const first = renderHook(() => usePublicCatalog(section, 12, null), { wrapper })
    await waitFor(() => expect(first.result.current.isSuccess).toBe(true))
    expect(catalog).toHaveBeenLastCalledWith(section, 0)
    first.unmount()
    const next = renderHook(() => usePublicCatalog(section, 12, null), { wrapper })
    await waitFor(() => expect(next.result.current.isSuccess).toBe(true))
    expect(catalog).toHaveBeenLastCalledWith(section, 12)
    expect(next.result.current.hasNextPage).toBe(true)
    await act(async () => {
      await next.result.current.fetchNextPage()
    })
    expect(catalog).toHaveBeenLastCalledWith(section, 24)
    await waitFor(() => expect(next.result.current.hasNextPage).toBe(false))
    client.clear()
  }
)

it('recovers when the cached random page no longer exists', async () => {
  localStorage.setItem('skillhub-public-page-counts-v1', JSON.stringify({ recommended: { pages: 10, at: Date.now() } }))
  vi.spyOn(Math, 'random').mockReturnValue(0.9)
  catalog.mockImplementation(async (_section, offset) => ({
    total: 15,
    skills: offset ? [] : [{ name: 'First' }],
    packages: []
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )

  const hook = renderHook(() => usePublicCatalog('recommended', 12, null), { wrapper })
  await waitFor(() => expect(hook.result.current.isSuccess).toBe(true))
  expect(catalog.mock.calls.map(call => call[1])).toEqual([108, 0])
  await act(async () => {
    await hook.result.current.fetchNextPage()
  })
  expect(catalog).toHaveBeenLastCalledWith('recommended', 12)
  client.clear()
})
