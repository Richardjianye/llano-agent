// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { MySkills } from './my-skills'

const mine = vi.hoisted(() => vi.fn())
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<object>()),
  getMyPublishedSkills: () => mine()
}))
vi.mock('./refine-publish-dialog', () => ({
  RefinePublishDialog: ({ skillName }: { skillName: string }) => <div>publish {skillName}</div>
}))
vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<object>()),
  $activeGatewayProfile: (await import('nanostores')).atom(null)
}))

function view(onPreview = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MySkills onPreview={onPreview} />
    </QueryClientProvider>
  )

  return onPreview
}

afterEach(() => {
  cleanup()
  mine.mockReset()
})

const listed = {
  slug: 'weekly-report',
  name: '周报',
  description: '汇总一周销售',
  version: 3,
  status: 'listed',
  scope: 'dept',
  scope_dept_name: '产品部',
  download_count: 4,
  usage_count: 9,
  user_count: 2,
  changelog: '补充核对步骤',
  published_at: '2026-10-15T09:30:00',
  local_name: 'weekly-report',
  local_changed: true
}

it('shows status, reach and unpublished local changes, and publishes a new version from the local copy', async () => {
  mine.mockResolvedValue({
    skills: [
      listed,
      {
        ...listed,
        scope: 'company',
        user_count: 0,
        changelog: '',
        slug: 'old-flow',
        name: '旧流程',
        status: 'delisted',
        delisted_reason: '流程已废止',
        local_name: null,
        local_changed: false
      }
    ]
  })
  const onPreview = view()

  expect(await screen.findByText('周报')).toBeTruthy()
  expect(screen.getByText('产品部')).toBeTruthy()
  expect(screen.getByText('Company pick')).toBeTruthy()
  expect(screen.getByText(/2 colleagues used it/)).toBeTruthy()
  expect(screen.getByText(/changed on this computer after it was published/)).toBeTruthy()
  expect(screen.getByText('Delisted: 流程已废止')).toBeTruthy()
  expect(screen.getAllByRole('button', { name: 'Publish new version' })).toHaveLength(1)

  fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
  expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({ identifier: 'llano-hub/weekly-report', mine: true }))

  fireEvent.click(screen.getByRole('button', { name: 'Publish new version' }))
  expect(screen.getByText('publish weekly-report')).toBeTruthy()
})

it('explains how to publish when there is nothing yet, and surfaces load failures', async () => {
  mine.mockResolvedValueOnce({ skills: [] })
  view()
  expect(await screen.findByText(/have not published any skills yet/)).toBeTruthy()
  cleanup()

  mine.mockRejectedValueOnce(new Error('连不上网关'))
  view()
  expect(await screen.findByText(/连不上网关/)).toBeTruthy()
})
