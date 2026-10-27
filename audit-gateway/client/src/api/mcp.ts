import type { ListResult } from '@/types/api'
import { http } from '@/api/http'

export interface McpConfigField {
  key: string
  label: string
  type: 'text' | 'password' | 'number'
  required: boolean
  secret: boolean
  default?: string | number
  min?: number
  max?: number
}

export interface McpAdapter {
  key: string
  name: string
  description: string
  config_schema: McpConfigField[]
  capability_count: number
}

export interface McpInstance {
  id: number
  code: string
  name: string
  adapter_key: string
  adapter_name: string
  config: Record<string, unknown>
  configured_secret_fields: string[]
  status: number
  remark?: string | null
}

export interface McpCapability {
  id: number
  code: string
  name: string
  domain: string
  description?: string
  method: string
  path: string
  input_schema: Record<string, unknown>
  metadata: Record<string, unknown>
  status: number
}

export interface McpGrant {
  id: number
  user_uid: string
  username: string
  capability_code: string
  capability_name: string
  domain: string
  effect: string
  constraints: Record<string, unknown>
  remark?: string | null
  expires_at?: string | null
  status: number
}

export interface McpCallLog {
  id: number
  request_id: string
  user_uid: string
  username: string
  capability_code: string
  status: string
  error_code?: string | null
  latency_ms: number
  response_size: number
  created_at: string
}

export const listMcpAdapters = () => http.get<ListResult<McpAdapter>>('/admin/mcp/adapters')
export const listMcpInstances = () => http.get<ListResult<McpInstance>>('/admin/mcp/instances')
export const createMcpInstance = (payload: Record<string, unknown>) => http.post<{ id: number }>('/admin/mcp/instances', payload)
export const updateMcpInstance = (id: number, payload: Record<string, unknown>) => http.post<Record<string, never>>(`/admin/mcp/instances/${id}`, payload)
export const syncMcpInstance = (id: number) => http.post<{ total: number, added: number, changed: number, renamed: number, unavailable: number }>(`/admin/mcp/instances/${id}/sync`)
export const setMcpCapabilityStatus = (instanceId: number, capabilityId: number, status: number) => http.post<Record<string, never>>(`/admin/mcp/instances/${instanceId}/capabilities/${capabilityId}/status?status=${status}`)
export const listMcpCapabilities = (id: number, query?: string, domain?: string) => http.get<ListResult<McpCapability>>(`/admin/mcp/instances/${id}/capabilities`, { query, domain })
export const listMcpPermissions = (id: number, userUid?: string) => http.get<ListResult<McpGrant>>(`/admin/mcp/instances/${id}/permissions`, { user_uid: userUid })
export const replaceMcpPermissions = (id: number, payload: Record<string, unknown>) => http.post<{ count: number }>(`/admin/mcp/instances/${id}/permissions`, payload)
export function revokeMcpPermissions (id: number, userUid: string) {
  return replaceMcpPermissions(id, {
    user_uid: userUid,
    capability_codes: [],
    effect: 'allow',
    constraints: {},
    replace: true,
  })
}
export const listMcpLogs = (id: number) => http.get<ListResult<McpCallLog>>(`/admin/mcp/instances/${id}/logs`)
