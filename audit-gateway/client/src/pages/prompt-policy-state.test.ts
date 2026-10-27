import type {
  PromptPolicyDraft,
  PromptPolicyOverview,
  PromptPolicyVersion,
} from '@/api/prompt-policy'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/http'
import {
  type PromptPolicyClient,
  usePromptPolicyState,
  validatePromptPolicyPackage,
} from '@/pages/prompt-policy-state'

function version (number: number, sourceVersion: number | null = null): PromptPolicyVersion {
  return {
    version: number,
    company_core_policy: `policy-${number}`,
    default_persona: `persona-${number}`,
    content_hash: `sha256:${number}`,
    published_at: `2026-09-${String(number).padStart(2, '0')}T00:00:00Z`,
    published_by: 'developer-1',
    published_by_uid: 'uid-developer-1',
    source_version: sourceVersion,
  }
}

function draft (revision: number, baseVersion: number | null): PromptPolicyDraft {
  return {
    company_core_policy: `draft-policy-${revision}`,
    default_persona: `draft-persona-${revision}`,
    content_hash: `sha256:draft-${revision}`,
    revision,
    base_version: baseVersion,
    updated_at: '2026-09-17T00:00:00Z',
    updated_by: 'developer-1',
    updated_by_uid: 'uid-developer-1',
  }
}

function overview (publishedVersion: number, revision: number): PromptPolicyOverview {
  return { published: version(publishedVersion), draft: draft(revision, publishedVersion) }
}

function fakeClient (initial: PromptPolicyOverview): PromptPolicyClient {
  return {
    fetchPromptPolicy: vi.fn().mockResolvedValue(initial),
    fetchPromptPolicyHistory: vi.fn().mockResolvedValue({
      items: initial.published ? [initial.published] : [],
      events: [],
    }),
    savePromptPolicyDraft: vi.fn(),
    publishPromptPolicy: vi.fn(),
    rollbackPromptPolicy: vi.fn(),
  }
}

describe('prompt policy state', () => {
  it('saves both policy blocks with the loaded revision and adopts the new revision', async () => {
    const client = fakeClient(overview(3, 8))
    vi.mocked(client.savePromptPolicyDraft).mockResolvedValue({ draft: draft(9, 3) })
    const state = usePromptPolicyState(client)

    await state.load()
    state.draft.company_core_policy = 'new policy'
    state.draft.default_persona = 'new persona'
    expect(state.dirty.value).toBe(true)

    await state.saveDraft()

    expect(client.savePromptPolicyDraft).toHaveBeenCalledWith({
      company_core_policy: 'new policy',
      default_persona: 'new persona',
      expected_revision: 8,
    })
    expect(state.draft.revision).toBe(9)
    expect(state.dirty.value).toBe(false)
  })

  it('refuses to publish unsaved edits', async () => {
    const client = fakeClient(overview(1, 2))
    const state = usePromptPolicyState(client)
    await state.load()
    state.draft.default_persona = 'unsaved'

    await expect(state.publish()).rejects.toThrow('请先保存')
    expect(client.publishPromptPolicy).not.toHaveBeenCalled()
  })

  it('publishes the saved revision and reloads the authoritative snapshot', async () => {
    const initial = overview(4, 10)
    const afterPublish = overview(5, 11)
    const client = fakeClient(initial)
    vi.mocked(client.publishPromptPolicy).mockResolvedValue({ published: version(5) })
    vi.mocked(client.fetchPromptPolicy)
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(afterPublish)
    vi.mocked(client.fetchPromptPolicyHistory)
      .mockResolvedValueOnce({ items: [version(4)], events: [] })
      .mockResolvedValueOnce({ items: [version(5), version(4)], events: [] })
    const state = usePromptPolicyState(client)

    await state.load()
    await state.publish()

    expect(client.publishPromptPolicy).toHaveBeenCalledWith(10)
    expect(state.published.value?.version).toBe(5)
    expect(state.history.value.map(item => item.version)).toEqual([5, 4])
    expect(state.needsRefresh.value).toBe(true)

    await state.load()

    expect(state.draft.revision).toBe(11)
    expect(state.needsRefresh.value).toBe(false)
  })

  it('rolls back against the current published version and reloads history', async () => {
    const initial = overview(6, 12)
    const afterRollback = overview(7, 13)
    afterRollback.published = version(7, 2)
    const client = fakeClient(initial)
    vi.mocked(client.rollbackPromptPolicy).mockResolvedValue({ published: version(7, 2) })
    vi.mocked(client.fetchPromptPolicy)
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(afterRollback)
    vi.mocked(client.fetchPromptPolicyHistory)
      .mockResolvedValueOnce({ items: [version(6)], events: [] })
      .mockResolvedValueOnce({
        items: [version(7, 2), version(6), version(2)],
        events: [],
      })
    const state = usePromptPolicyState(client)

    await state.load()
    await state.rollback(2)

    expect(client.rollbackPromptPolicy).toHaveBeenCalledWith(2, 6)
    expect(state.published.value).toMatchObject({ version: 7, source_version: 2 })
    expect(state.history.value[0]).toMatchObject({ version: 7, source_version: 2 })
    expect(state.needsRefresh.value).toBe(true)

    await state.load()

    expect(state.needsRefresh.value).toBe(false)
  })

  it('allows an incomplete draft to be saved but requires both blocks for publishing', async () => {
    expect(validatePromptPolicyPackage('', 'persona')).toEqual(['公司统一规则不能为空'])
    expect(validatePromptPolicyPackage('policy', '  ')).toEqual(['默认人格不能为空'])
    expect(validatePromptPolicyPackage('policy', 'persona')).toEqual([])

    const initial = overview(1, 3)
    initial.draft.company_core_policy = ''
    const client = fakeClient(initial)
    vi.mocked(client.savePromptPolicyDraft).mockResolvedValue({ draft: initial.draft })
    const state = usePromptPolicyState(client)
    await state.load()

    await expect(state.saveDraft()).resolves.toMatchObject({ company_core_policy: '' })
    await expect(state.publish()).rejects.toThrow('公司统一规则不能为空')
    expect(client.publishPromptPolicy).not.toHaveBeenCalled()
  })

  it('locks all writes after a stale draft conflict until an authoritative reload', async () => {
    const initial = overview(3, 8)
    const refreshed = overview(3, 9)
    const client = fakeClient(initial)
    vi.mocked(client.savePromptPolicyDraft).mockRejectedValue(
      new ApiError(10_005, '草稿已被其他人更新，请刷新后重试', {
        current_revision: 9,
      }),
    )
    vi.mocked(client.fetchPromptPolicy)
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(refreshed)
    const state = usePromptPolicyState(client)
    await state.load()
    state.draft.company_core_policy = 'stale local edit'

    await expect(state.saveDraft()).rejects.toThrow('请刷新后重试')

    expect(state.needsRefresh.value).toBe(true)
    await expect(state.saveDraft()).rejects.toThrow('需要刷新')
    expect(client.savePromptPolicyDraft).toHaveBeenCalledTimes(1)

    await state.load()
    expect(state.needsRefresh.value).toBe(false)
    expect(state.draft.revision).toBe(9)
  })

  it('locks writes after publish or rollback version conflicts', async () => {
    const publishClient = fakeClient(overview(4, 10))
    vi.mocked(publishClient.publishPromptPolicy).mockRejectedValue(
      new ApiError(10_005, '草稿已被其他人更新，请刷新后重试', {
        current_revision: 11,
      }),
    )
    const publishState = usePromptPolicyState(publishClient)
    await publishState.load()

    await expect(publishState.publish()).rejects.toThrow('请刷新后重试')
    expect(publishState.needsRefresh.value).toBe(true)

    const rollbackClient = fakeClient(overview(6, 12))
    vi.mocked(rollbackClient.rollbackPromptPolicy).mockRejectedValue(
      new ApiError(10_005, '当前发布版本已变化，请刷新后重试', {
        current_version: 7,
      }),
    )
    const rollbackState = usePromptPolicyState(rollbackClient)
    await rollbackState.load()

    await expect(rollbackState.rollback(2)).rejects.toThrow('请刷新后重试')
    expect(rollbackState.needsRefresh.value).toBe(true)
  })

  it('keeps writes locked when a reload fails after the page was already loaded', async () => {
    const client = fakeClient(overview(2, 5))
    const state = usePromptPolicyState(client)
    await state.load()
    vi.mocked(client.fetchPromptPolicyHistory).mockRejectedValueOnce(
      new Error('gateway unavailable'),
    )

    await expect(state.load()).rejects.toThrow('gateway unavailable')

    expect(state.loaded.value).toBe(false)
    await expect(state.publish()).rejects.toThrow('尚未加载')
    expect(client.publishPromptPolicy).not.toHaveBeenCalled()
  })
})
