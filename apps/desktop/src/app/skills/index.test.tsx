// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import type * as ReactRouterDom from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { queryClient } from '@/lib/query-client'

const getSkills = vi.fn()
const completeLocalSkillMetadata = vi.fn()
const getToolsets = vi.fn()
const setSkillEnabled = vi.fn()
const setToolsetEnabled = vi.fn()
const getToolsetConfig = vi.fn()
const selectToolsetProvider = vi.fn()
const getUsageAnalytics = vi.fn()

// Partial mock: keep the real module (SkillsView pulls in @/store/profile,
// whose import-time subscription calls setApiRequestProfile) and stub only the
// calls we assert on.
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getSkills: () => getSkills(),
  completeLocalSkillMetadata: () => completeLocalSkillMetadata(),
  getToolsets: () => getToolsets(),
  setSkillEnabled: (name: string, enabled: boolean) => setSkillEnabled(name, enabled),
  setToolsetEnabled: (name: string, enabled: boolean) => setToolsetEnabled(name, enabled),
  getToolsetConfig: (name: string) => getToolsetConfig(name),
  selectToolsetProvider: (toolset: string, provider: string) => selectToolsetProvider(toolset, provider),
  getUsageAnalytics: (days: number) => getUsageAnalytics(days)
}))

// Notifications hit nanostores/timers we don't care about here.
vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

// The vision detail navigates to Settings → Models via useNavigate; spy on it
// so the deep-link target is assertable.
const navigateSpy = vi.fn()

vi.mock('react-router', async importOriginal => ({
  ...(await importOriginal<typeof ReactRouterDom>()),
  useNavigate: () => navigateSpy
}))

function toolset(overrides: Record<string, unknown> = {}) {
  return {
    name: 'web',
    label: 'Web Search',
    description: 'web_search, web_extract',
    enabled: true,
    available: true,
    configured: true,
    tools: ['web_search', 'web_extract'],
    ...overrides
  }
}

async function renderSkills(entry = '/skills?tab=toolsets') {
  const { SkillsView } = await import('./index')
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      // SkillsView reads skills/toolsets via useQuery, so it needs a provider.
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[entry]}>
          <SkillsView />
        </MemoryRouter>
      </QueryClientProvider>
    )
  })

  return result!
}

beforeEach(() => {
  getSkills.mockResolvedValue([])
  completeLocalSkillMetadata.mockResolvedValue({ queued: 1 })
  getToolsets.mockResolvedValue([toolset()])
  setToolsetEnabled.mockResolvedValue({ ok: true, name: 'web', enabled: false })
  getToolsetConfig.mockResolvedValue({ has_category: true, active_provider: null, providers: [] })
  getUsageAnalytics.mockResolvedValue({ tools: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  // Shared singleton client — drop cached skills/toolsets so each test refetches.
  queryClient.clear()
})

describe('SkillsView toolset management', () => {
  it('renders a switch for each toolset and toggles it off', async () => {
    await renderSkills()

    // The switch names the action, so an enabled toolset offers to turn it off.
    const sw = await screen.findByRole('switch', { name: 'Turn Web Search toolset off' })
    expect(sw.getAttribute('aria-checked')).toBe('true')

    await act(async () => {
      fireEvent.click(sw)
    })

    await waitFor(() => expect(setToolsetEnabled).toHaveBeenCalledWith('web', false))
  })

  it('renders toolset titles without leading emoji', async () => {
    getToolsets.mockResolvedValue([toolset({ name: 'cronjob', label: '⏰ Cron Jobs', description: 'cron tools' })])

    await renderSkills()

    // The label renders in both the row and the auto-selected detail header, so
    // assert via the switch's (emoji-stripped) accessible name and the absence
    // of the emoji rather than a single-match text lookup.
    await screen.findByRole('switch', { name: 'Turn Cron Jobs toolset off' })
    expect(screen.queryByText(/⏰/)).toBeNull()
  })

  it('renders the provider config panel inline for the selected toolset', async () => {
    // The master-detail UI dropped the resting "Configured" pill and the
    // "Configure" expander: the detail column auto-selects the first toolset
    // and renders its config panel directly, which fetches on mount.
    await renderSkills()

    await screen.findByRole('switch', { name: 'Turn Web Search toolset off' })
    await waitFor(() => expect(getToolsetConfig).toHaveBeenCalledWith('web'))
  })

  it('shows a vision explainer that deep-links to Settings → Models', async () => {
    // Vision has no TOOL_CATEGORIES provider matrix — its model lives in the
    // auxiliary model config, so the detail pane must point there instead of
    // rendering an empty panel.
    getToolsets.mockResolvedValue([
      toolset({
        name: 'vision',
        label: 'Vision / Image Analysis',
        description: 'vision_analyze',
        tools: ['vision_analyze']
      })
    ])
    getToolsetConfig.mockResolvedValue({ has_category: false, active_provider: null, providers: [] })

    await renderSkills()

    expect(await screen.findByText(/auxiliary model configuration/)).toBeTruthy()
    const link = screen.getByRole('button', { name: /Choose vision model in Settings/ })

    await act(async () => {
      fireEvent.click(link)
    })

    // Internal route change into the Models section with the aux slot target —
    // consumed by ModelSettings' deep-link highlight. Never an external URL.
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/settings?tab=config:model&aux=vision'))
  })
})

// CUSTOM（决策 #59）：中文展示名 + 技能页的「＋ 添加技能」入口
describe('SkillsView skills tab', () => {
  const skills = [
    {
      name: 'invoice-helper',
      display_name: '发票助手',
      description: '处理中文发票识别。',
      category: 'general',
      enabled: true,
      provenance: 'agent'
    },
    {
      name: 'plain-english',
      description: 'A plain bundled skill.',
      category: 'general',
      enabled: true,
      provenance: 'bundled'
    }
  ]

  it('shows the Chinese display name as the title and keeps the slug as a small label', async () => {
    getSkills.mockResolvedValue(skills)

    await renderSkills('/skills?tab=skills')

    // 行标题与自动选中的详情标题都是中文名
    expect((await screen.findAllByText('发票助手')).length).toBeGreaterThanOrEqual(2)
    // 英文标识仍看得到（/invoice-helper 是它的命令名），但不再是标题
    expect(screen.getAllByText('invoice-helper').length).toBeGreaterThan(0)
    // 没写展示名的技能回落到 name
    expect(screen.getAllByText('plain-english').length).toBeGreaterThan(0)
  })

  it('puts the add-skill entry on the skills tab, with the "only on your computer" note beside it', async () => {
    getSkills.mockResolvedValue(skills)

    await renderSkills('/skills?tab=skills')

    expect(await screen.findByRole('button', { name: '+ Add skill' })).toBeTruthy()
    // 说明就在按钮旁边：这句话缺席，正是原来那个入口被当成「上传到技能中心」的原因
    expect(screen.getByText(/to your own computer only/i)).toBeTruthy()
  })

  it('offers the add-skill entry in the empty state too', async () => {
    getSkills.mockResolvedValue([])

    await renderSkills('/skills?tab=skills')

    expect(await screen.findByRole('button', { name: '+ Add skill' })).toBeTruthy()
  })
})

describe('skill list helpers', () => {
  it('labels by display_name and falls back to name', async () => {
    const { skillLabel } = await import('./index')

    expect(skillLabel({ category: 'x', description: '', enabled: true, name: 'a', display_name: '甲' })).toBe('甲')
    expect(skillLabel({ category: 'x', description: '', enabled: true, name: 'a', display_name: '  ' })).toBe('a')
    expect(skillLabel({ category: 'x', description: '', enabled: true, name: 'a' })).toBe('a')
  })

  it('finds a skill by its Chinese display name', async () => {
    const { filteredSkills } = await import('./index')

    const rows = [
      {
        category: 'general',
        description: 'Extract fields.',
        display_name: '发票助手',
        enabled: true,
        name: 'invoice-helper'
      },
      { category: 'general', description: 'Other.', enabled: true, name: 'other' }
    ]

    expect(filteredSkills(rows, '发票', true).map(r => r.name)).toEqual(['invoice-helper'])
    expect(filteredSkills(rows, 'other', true).map(r => r.name)).toEqual(['other'])
  })
})

describe('Chinese skill categories', () => {
  it('renders known category labels in Chinese, preserves Chinese custom categories and hides unknown English labels', async () => {
    const { skillCategoryLabel } = await import('./index')
    expect(skillCategoryLabel('productivity')).toBe('办公效率')
    expect(skillCategoryLabel('software-development')).toBe('软件开发')
    expect(skillCategoryLabel('自定义分类')).toBe('自定义分类')
    expect(skillCategoryLabel('unknown-custom-category')).toBe('其他技能')
    getSkills.mockResolvedValue([
      {
        name: 'pdf',
        display_name: '便携文档处理',
        description: '处理便携文档。',
        category: 'productivity',
        enabled: true
      }
    ])
    await renderSkills('/skills?tab=skills')
    expect((await screen.findAllByText('办公效率')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Productivity')).toBeNull()
  })
})

describe('local skill Chinese metadata', () => {
  // 2026-10-16：没有中文时后端回原文，补全由按钮发起
  const incomplete = {
    name: 'learned-report',
    display_name: 'learned-report',
    description: 'Summarise the weekly sales report',
    category: 'productivity',
    enabled: true,
    provenance: 'agent',
    metadata_incomplete: true,
    metadata_completable: true
  }

  it('opens on the skill center tab by default', async () => {
    await renderSkills('/skills')
    expect(await screen.findByRole('button', { name: 'My published skills' })).toBeTruthy()
    expect(getSkills).toHaveBeenCalled()
  })

  it('shows the original text and offers AI completion with a count, without calling AI on its own', async () => {
    getSkills.mockResolvedValue([
      incomplete,
      {
        ...incomplete,
        name: 'company-skill',
        display_name: 'company-skill',
        provenance: 'hub',
        metadata_completable: false
      }
    ])
    await renderSkills('/skills?tab=skills')
    expect((await screen.findAllByText('learned-report')).length).toBeGreaterThan(0)
    expect(await screen.findByRole('button', { name: 'Fill in Chinese with AI (1)' })).toBeTruthy()
    expect(completeLocalSkillMetadata).not.toHaveBeenCalled()
  })

  it('refreshes a pending completion into its saved Chinese title', async () => {
    getSkills.mockResolvedValue([{ ...incomplete, metadata_pending: true }])
    await renderSkills('/skills?tab=skills')
    expect(
      await screen.findByText('AI is completing Chinese titles and descriptions; results are saved automatically')
    ).toBeTruthy()
    getSkills.mockResolvedValue([
      { ...incomplete, display_name: '销售报表汇总', metadata_pending: false, metadata_incomplete: false }
    ])
    await waitFor(() => expect(screen.getAllByText('销售报表汇总').length).toBeGreaterThan(0), { timeout: 4000 })
    expect(
      screen.queryByText('AI is completing Chinese titles and descriptions; results are saved automatically')
    ).toBeNull()
    expect(completeLocalSkillMetadata).not.toHaveBeenCalled()
  })

  it('retries failed completion without opening an editor', async () => {
    getSkills.mockResolvedValue([incomplete])
    await renderSkills('/skills?tab=skills')
    fireEvent.click(await screen.findByRole('button', { name: 'Fill in Chinese with AI (1)' }))
    await waitFor(() => expect(completeLocalSkillMetadata).toHaveBeenCalledOnce())
    await waitFor(() => expect(getSkills.mock.calls.length).toBeGreaterThan(1))
  })
})
