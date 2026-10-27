import { beforeEach, describe, expect, it, vi } from 'vitest'
import { clearTokens } from '@/api/http'
import {
  fetchPromptPolicy,
  fetchPromptPolicyHistory,
  publishPromptPolicy,
  rollbackPromptPolicy,
  savePromptPolicyDraft,
} from '@/api/prompt-policy'

function apiResponse (
  data: unknown = {},
  code = 0,
  message = '',
  status = 200,
): Response {
  return Response.json({ code, data, message }, {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('prompt policy API', () => {
  beforeEach(() => {
    clearTokens()
    vi.restoreAllMocks()
  })

  it('uses the versioned management endpoints and concurrency guards', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => apiResponse({}))
    vi.stubGlobal('fetch', fetchMock)

    await fetchPromptPolicy()
    await savePromptPolicyDraft({
      company_core_policy: 'policy',
      default_persona: 'persona',
      expected_revision: 4,
    })
    await publishPromptPolicy(5)
    await fetchPromptPolicyHistory()
    await rollbackPromptPolicy(2, 7)

    expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
      '/admin/prompt-policy',
      '/admin/prompt-policy/draft',
      '/admin/prompt-policy/publish',
      '/admin/prompt-policy/history',
      '/admin/prompt-policy/rollback/2',
    ])

    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      method: 'PUT',
      body: JSON.stringify({
        company_core_policy: 'policy',
        default_persona: 'persona',
        expected_revision: 4,
      }),
    })
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ expected_revision: 5 }),
    })
    expect(fetchMock.mock.calls[4]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ expected_current_version: 7 }),
    })
  })

  it('preserves the backend conflict message for an explicit refresh prompt', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(apiResponse(
      { current_revision: 9 },
      10_005,
      '草稿已被其他人更新，请刷新后重试',
      409,
    )))

    await expect(savePromptPolicyDraft({
      company_core_policy: 'policy',
      default_persona: 'persona',
      expected_revision: 8,
    })).rejects.toMatchObject({
      code: 10_005,
      message: '草稿已被其他人更新，请刷新后重试',
      data: { current_revision: 9 },
    })
  })
})
