/** 公司提示词策略管理 —— 对应 server/router/admin/prompt_policy.py */

import { http } from '@/api/http'

export interface PromptPolicyVersion {
  version: number
  company_core_policy: string
  default_persona: string
  content_hash: string
  published_at: string
  published_by: string
  published_by_uid: string
  /** 普通发布为 null；回滚版本记录被恢复的历史版本 */
  source_version: number | null
}

export interface PromptPolicyDraft {
  company_core_policy: string
  default_persona: string
  content_hash: string | null
  /** 乐观锁版本；保存与发布都必须带回，避免覆盖别人的修改 */
  revision: number
  base_version: number | null
  updated_at: string | null
  updated_by: string | null
  updated_by_uid: string | null
}

export interface PromptPolicyOverview {
  published: PromptPolicyVersion | null
  draft: PromptPolicyDraft
}

export interface PromptPolicyHistoryEvent {
  id: number
  action: string
  actor_uid: string
  actor_name: string
  content_hash: string
  draft_revision: number | null
  version: number | null
  source_version: number | null
  created_at: string
}

export interface PromptPolicyHistory {
  items: PromptPolicyVersion[]
  events: PromptPolicyHistoryEvent[]
}

export interface PromptPolicyDraftUpdate {
  company_core_policy: string
  default_persona: string
  expected_revision: number
}

export function fetchPromptPolicy () {
  return http.get<PromptPolicyOverview>('/admin/prompt-policy')
}

export function savePromptPolicyDraft (payload: PromptPolicyDraftUpdate) {
  return http.put<{ draft: PromptPolicyDraft }>('/admin/prompt-policy/draft', payload)
}

export function publishPromptPolicy (expectedRevision: number) {
  return http.post<{ published: PromptPolicyVersion }>('/admin/prompt-policy/publish', {
    expected_revision: expectedRevision,
  })
}

export function fetchPromptPolicyHistory () {
  return http.get<PromptPolicyHistory>('/admin/prompt-policy/history')
}

export function rollbackPromptPolicy (version: number, expectedCurrentVersion: number) {
  return http.post<{ published: PromptPolicyVersion }>(
    `/admin/prompt-policy/rollback/${version}`,
    { expected_current_version: expectedCurrentVersion },
  )
}
