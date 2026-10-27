// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { SkillPublishCheck, SkillRefineDraft, SkillRefineFinding } from '@/hermes'

import { publishHintLabel } from './publish-hint'
import { RefinePublishDialog, unresolvedFindings } from './refine-publish-dialog'

const api = vi.hoisted(() => ({ check: vi.fn(), refine: vi.fn(), publish: vi.fn() }))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<object>()),
  checkSkillPublish: (...args: unknown[]) => api.check(...args),
  refineSkillDraft: (...args: unknown[]) => api.refine(...args),
  publishRefinedSkill: (...args: unknown[]) => api.publish(...args)
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const ORIGINAL = '---\nname: weekly-report\n---\n打电话 13812345678 给 ACME\n'

const FINDINGS: SkillRefineFinding[] = [
  {
    id: 'rule-1',
    source: 'rule',
    kind: 'phone',
    label: '手机号',
    text: '13812345678',
    placeholder: '{手机号}',
    file: 'SKILL.md',
    line: 4
  },
  {
    id: 'ai-2',
    source: 'ai',
    kind: 'business',
    label: '客户名',
    text: 'ACME',
    placeholder: '{客户名}',
    file: 'SKILL.md',
    line: null
  },
  {
    id: 'rule-3',
    source: 'rule',
    kind: 'email',
    label: '邮箱',
    text: 'a@b.cn',
    placeholder: '{邮箱}',
    file: 'refs/contact.md',
    line: 2
  }
]

function checked(overrides: Partial<SkillPublishCheck> = {}): SkillPublishCheck {
  return {
    name: 'weekly-report',
    slug: 'weekly-report',
    status: { status: 'available' },
    original: ORIGINAL,
    findings: [FINDINGS[0]!, FINDINGS[2]!],
    skipped: [],
    file_count: 2,
    lint: [],
    spec_issues: ['正文缺少发布规范要求的章节：步骤。'],
    scan: { verdict: 'safe', findings: [] },
    conforms: false,
    checked_at: 1_760_000_000,
    cached: false,
    ...overrides
  }
}

function draft(overrides: Partial<SkillRefineDraft> = {}): SkillRefineDraft {
  return {
    original: ORIGINAL,
    draft: '---\nname: weekly-report\ndisplay_name: "周报"\n---\n## 步骤\n打电话 13812345678 给 ACME\n',
    findings: FINDINGS,
    changelog: '首次发布',
    ai_error: '',
    refined_at: 1_760_000_030,
    cached: false,
    ...overrides
  }
}

function view() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RefinePublishDialog displayName="周报" onClose={vi.fn()} skillName="weekly-report" />
    </QueryClientProvider>
  )
}

it('counts a finding as handled once it is gone from SKILL.md or explicitly kept', () => {
  const findings = FINDINGS

  expect(unresolvedFindings(findings, 'nothing left', new Set())).toEqual([findings[2]])
  expect(unresolvedFindings(findings, 'nothing left', new Set(['a@b.cn']))).toEqual([])
  expect(unresolvedFindings(findings, '13812345678', new Set())).toHaveLength(2)
})

it('runs the machine check before the AI, showing progress, and gates publishing on every finding', async () => {
  let finishRefine: (value: SkillRefineDraft) => void = () => undefined
  api.check.mockResolvedValue(checked())
  api.refine.mockReturnValue(new Promise(resolve => (finishRefine = resolve)))
  api.publish.mockResolvedValue({
    success: true,
    published: true,
    slug: 'weekly-report',
    version: 1,
    file_count: 2,
    message: '已发布「周报」v1',
    local_backup: '/skills/.archive/weekly-report.before-refine'
  })
  view()

  // 机器检查做完、AI 还在跑：第二步显示进行中，审阅区还没出来
  await waitFor(() => expect(api.refine).toHaveBeenCalledWith('weekly-report', false))
  expect(api.check).toHaveBeenCalledWith('weekly-report', false)
  expect(screen.getAllByText('Done')).toHaveLength(1)
  expect(screen.getByText(/In progress, \d+s so far/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Publish' })).toBeNull()

  await act(async () => finishRefine(draft()))
  const publish = await screen.findByRole('button', { name: 'Publish' })
  expect(publish.hasAttribute('disabled')).toBe(true)
  expect(screen.getByText('3 sensitive items still to handle')).toBeTruthy()
  expect(screen.getByText('Publishing standard notes (1)')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: 'Replace with {手机号}' }))
  fireEvent.click(screen.getByRole('button', { name: 'Replace with {客户名}' }))
  fireEvent.click(screen.getByRole('button', { name: 'Keep (public)' }))
  expect(publish.hasAttribute('disabled')).toBe(false)

  await act(async () => fireEvent.click(publish))
  const sent = api.publish.mock.calls[0]![0]
  expect(sent.skillMd).toContain('打电话 {手机号} 给 {客户名}')
  expect(sent).toMatchObject({ name: 'weekly-report', kept: ['a@b.cn'], replaceLocal: true, changelog: '首次发布' })
  expect(sent.slug).toBeUndefined()
  expect(await screen.findByText(/Original backed up to/)).toBeTruthy()
})

it('skips the AI for a skill that already meets the standard, unless asked', async () => {
  const clean = '---\nname: weekly-report\ndisplay_name: "周报"\n---\n## 步骤\n打开报表\n'
  api.check.mockResolvedValue(checked({ conforms: true, findings: [], spec_issues: [], original: clean }))
  api.refine.mockResolvedValue(draft({ findings: [] }))
  view()

  const publish = await screen.findByRole('button', { name: 'Publish' })
  expect(screen.getByText('Already meets the publishing standard')).toBeTruthy()
  expect(api.refine).not.toHaveBeenCalled()
  expect(publish.hasAttribute('disabled')).toBe(false)

  await act(async () => fireEvent.click(publish))
  expect(api.publish.mock.calls[0]![0].skillMd).toBe(clean)
  cleanup()
  api.publish.mockClear()

  view()
  fireEvent.click(await screen.findByRole('button', { name: 'Tidy up with AI anyway' }))
  await waitFor(() => expect(api.refine).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(screen.getAllByText('Done')).toHaveLength(2))
})

it('reuses the last review until the user asks to check again', async () => {
  api.check.mockResolvedValue(checked({ cached: true }))
  api.refine.mockResolvedValue(draft({ cached: true }))
  view()

  await screen.findByRole('button', { name: 'Publish' })
  expect(screen.getAllByText('Reused last result')).toHaveLength(2)
  expect(screen.getByText(/has not changed, so the last check is reused/)).toBeTruthy()

  api.check.mockResolvedValue(checked())
  api.refine.mockResolvedValue(draft())
  fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
  await waitFor(() => expect(api.refine).toHaveBeenLastCalledWith('weekly-report', true))
  expect(api.check).toHaveBeenLastCalledWith('weekly-report', true)
  await waitFor(() => expect(screen.getAllByText('Done')).toHaveLength(2))
})

it('requires a new publishing ID when the old one belongs to someone else, and skips the AI for delisted skills', async () => {
  api.check.mockResolvedValueOnce(checked({ findings: [], status: { status: 'taken', owner_username: '张三' } }))
  api.refine.mockResolvedValue(draft({ findings: [] }))
  view()
  const publish = await screen.findByRole('button', { name: 'Publish' })
  expect(screen.getByText(/already used by 张三/)).toBeTruthy()
  expect(publish.hasAttribute('disabled')).toBe(true)
  fireEvent.change(screen.getByRole('textbox', { name: /New publishing ID/ }), {
    target: { value: 'weekly-report-east' }
  })
  expect(publish.hasAttribute('disabled')).toBe(false)
  cleanup()
  api.refine.mockClear()

  api.check.mockResolvedValueOnce(checked({ findings: [], status: { status: 'delisted', delisted_reason: '重复' } }))
  view()
  expect((await screen.findByRole('button', { name: 'Publish' })).hasAttribute('disabled')).toBe(true)
  expect(screen.getByText('Delisted, skipped')).toBeTruthy()
  expect(api.refine).not.toHaveBeenCalled()
})

it('shows a retry when the check fails, without calling the AI', async () => {
  api.check.mockRejectedValueOnce(new Error('连不上网关')).mockResolvedValueOnce(checked())
  api.refine.mockResolvedValue(draft())
  view()
  expect(await screen.findByText('连不上网关')).toBeTruthy()
  expect(api.refine).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(api.check).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(api.refine).toHaveBeenCalledTimes(1))
})

it('marks skills worth sharing, and unpublished changes before that', () => {
  const r = { changedBadge: 'Unpublished changes', suggestBadge: 'Worth sharing' }
  const base = { name: 'x', description: '', enabled: true, category: 'x' }

  expect(
    publishHintLabel(
      { ...base, publish_state: { published: false, slug: null, local_changed: false, suggest: true } },
      r
    )
  ).toBe('Worth sharing')
  expect(
    publishHintLabel({ ...base, publish_state: { published: true, slug: 'x', local_changed: true, suggest: false } }, r)
  ).toBe('Unpublished changes')
  expect(publishHintLabel(base, r)).toBeNull()
})
