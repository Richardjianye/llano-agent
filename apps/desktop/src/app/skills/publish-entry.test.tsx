// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { queryClient } from '@/lib/query-client'

/**
 * CUSTOM（阶段 22）：个人技能空间的发布入口。
 *
 * 这一组守的是一件**不会报错的**事：
 *
 * 哪些技能能发 —— 发错对象不会抛异常，网关会用一句「只有首发人能发新版」
 * 把它挡回来，而员工看到的是一个他看不懂的错误。发布结果的显示见
 * refine-publish-dialog.test.tsx。
 */

const getSkills = vi.fn()
const getToolsets = vi.fn()
const getUsageAnalytics = vi.fn()
const dismissShareSuggestion = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getSkills: () => getSkills(),
  getToolsets: () => getToolsets(),
  getUsageAnalytics: (days: number) => getUsageAnalytics(days),
  dismissShareSuggestion: (name: string) => dismissShareSuggestion(name)
}))

vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))

function skill(overrides: Record<string, unknown> = {}) {
  return {
    name: 'lingxing-daily-report',
    display_name: '领星日报',
    description: '每天拉一次领星日报',
    category: 'general',
    enabled: true,
    usage: 0,
    ...overrides
  }
}

async function renderSkillsTab() {
  const { SkillsView } = await import('./index')

  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/skills?tab=skills']}>
          <SkillsView />
        </MemoryRouter>
      </QueryClientProvider>
    )
  })
}

// 2026-10-16 起详情里只有「整理并发布」一个发布入口（「原样发布」已移除）。
const publishAsIsButton = () => screen.queryByRole('button', { name: 'Publish as is' })
const refineButton = () => screen.queryByRole('button', { name: 'Tidy up and publish' })

beforeEach(() => {
  getToolsets.mockResolvedValue([])
  getUsageAnalytics.mockResolvedValue({ tools: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  queryClient.clear()
})

describe('谁能发布', () => {
  it('习得的技能可以发', async () => {
    getSkills.mockResolvedValue([skill({ provenance: 'agent' })])

    await renderSkillsTab()

    await waitFor(() => expect(refineButton()).not.toBeNull())
    expect(publishAsIsButton()).toBeNull()
  })

  it('手写的技能（没有 provenance）也可以发', async () => {
    // 发布的门槛比编辑宽一档：手写的技能是这个员工的工作成果，
    // 恰恰是最该进部门技能空间的那一类。
    getSkills.mockResolvedValue([skill()])

    await renderSkillsTab()

    await waitFor(() => expect(refineButton()).not.toBeNull())
  })

  it('从 hub 装下来的技能不能再发一次', async () => {
    // **这条是这组里最重要的一条。**
    //
    // 从 hub 装下来的技能包括从我们自己的 llano-hub 装的。把它再发一次，要么
    // 撞上网关的「只有首发人能发新版」，要么把别人的技能挂到自己名下 ——
    // 两个结果都不是员工按下按钮时想要的，而按钮存在本身就是在邀请他去按。
    getSkills.mockResolvedValue([skill({ provenance: 'hub' })])

    await renderSkillsTab()

    // 名字在列表行和详情标题里各出现一次，所以用 findAll —— 等的是「详情面板
    // 已经渲染出来了」，此时按钮该在的话就已经在了。
    await waitFor(() => expect(screen.getAllByText('领星日报').length).toBeGreaterThan(1))
    expect(refineButton()).toBeNull()
  })

  it('内置技能不能发', async () => {
    // 上游随包发的，发到公司库里只是噪音。
    getSkills.mockResolvedValue([skill({ provenance: 'bundled' })])

    await renderSkillsTab()

    // 名字在列表行和详情标题里各出现一次，所以用 findAll —— 等的是「详情面板
    // 已经渲染出来了」，此时按钮该在的话就已经在了。
    await waitFor(() => expect(screen.getAllByText('领星日报').length).toBeGreaterThan(1))
    expect(refineButton()).toBeNull()
  })
})

describe('「值得分享」提示', () => {
  const suggested = (usage: number) =>
    skill({
      provenance: 'agent',
      usage,
      publish_state: { published: false, slug: null, local_changed: false, suggest: true }
    })

  it('可以按技能「不再提示」，立即隐藏', async () => {
    let finish: (value: { ok: boolean }) => void = () => undefined
    getSkills.mockResolvedValue([suggested(5)])
    dismissShareSuggestion.mockReturnValue(new Promise(resolve => (finish = resolve)))
    await renderSkillsTab()

    const dismiss = await screen.findByRole('button', { name: "Don't remind me" })
    expect(screen.getByText(/Used in 5 sessions/)).toBeTruthy()
    fireEvent.click(dismiss)

    // 请求还没回来就先隐藏
    expect(dismissShareSuggestion).toHaveBeenCalledWith('lingxing-daily-report')
    await waitFor(() => expect(screen.queryByText(/Used in 5 sessions/)).toBeNull())
    expect(screen.queryByText('Worth sharing')).toBeNull()

    // 后端记住之后，刷新列表也不再提示
    getSkills.mockResolvedValue([
      { ...suggested(5), publish_state: { published: false, slug: null, local_changed: false, suggest: false } }
    ])
    await act(async () => finish({ ok: true }))
    expect(screen.queryByText(/Used in 5 sessions/)).toBeNull()
  })

  it('请求失败时提示恢复显示', async () => {
    getSkills.mockResolvedValue([suggested(4)])
    dismissShareSuggestion.mockRejectedValue(new Error('boom'))
    await renderSkillsTab()

    await act(async () => fireEvent.click(await screen.findByRole('button', { name: "Don't remind me" })))
    await waitFor(() => expect(screen.getByText(/Used in 4 sessions/)).toBeTruthy())
  })
})
