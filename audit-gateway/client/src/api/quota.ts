import type { ListResult, TokenQuotaRule } from '@/types/api'
import { http } from '@/api/http'

export interface TokenQuotaPayload {
  id?: number
  scope: 'user' | 'dept'
  scope_value: string
  model_id: number
  daily_token_limit: number
  status: number
  remark?: string
}

export function listQuotas () {
  return http.get<ListResult<TokenQuotaRule>>('/admin/quota/list')
}

export function saveQuota (payload: TokenQuotaPayload) {
  return http.post<{ id: number }>('/admin/quota/save', payload)
}

export function deleteQuota (id: number) {
  return http.post<Record<string, never>>(`/admin/quota/delete/${id}`)
}
