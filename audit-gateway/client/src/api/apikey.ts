/** API Key 管理接口 —— 对应 server/router/admin/apikey.py */

import type { ApiKeyCreateResult, ApiKeyOverviewRecord, ApiKeyRecord, ApiKeyState, ListResult } from '@/types/api'
import { http } from '@/api/http'

export interface ApiKeyCreatePayload {
  name: string
  valid_days?: number
  expires_at?: string
  total_token_limit?: number
  target_uid?: string
}

export function listApiKeys (uid?: string) {
  return http.get<ListResult<ApiKeyRecord>>('/admin/apikey/list', { uid })
}

export function createApiKey (payload: ApiKeyCreatePayload) {
  return http.post<ApiKeyCreateResult>('/admin/apikey/create', payload)
}

export function revokeApiKey (keyId: number) {
  return http.post<Record<string, never>>(`/admin/apikey/revoke/${keyId}`)
}

// ---------------- 全公司总览（apikey:admin）----------------

export interface ApiKeyOverviewQuery {
  states?: ApiKeyState[]
  keyword?: string
  limit?: number
  offset?: number
}

export function apiKeyOverview (query: ApiKeyOverviewQuery = {}) {
  return http.get<{ items: ApiKeyOverviewRecord[], total: number, idle_days: number }>(
    '/admin/apikey/overview',
    {
      state: query.states?.length ? query.states.join(',') : undefined,
      keyword: query.keyword || undefined,
      limit: query.limit ?? 50,
      offset: query.offset ?? 0,
    },
  )
}

export function batchRevokeApiKeys (ids: number[]) {
  return http.post<{ revoked: number, denied_ids: number[] }>('/admin/apikey/batch-revoke', { ids })
}

export function revokeIdleApiKeys (dryRun: boolean) {
  return http.post<{ revoked?: number, would_revoke?: number, denied_ids: number[], idle_days: number }>(
    '/admin/apikey/revoke-idle',
    { dry_run: dryRun },
  )
}
