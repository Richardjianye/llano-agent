/** 模型目录与白名单接口 —— 对应 server/router/admin/catalog.py */

import type {
  ListResult,
  ModelAdminInfo,
  ModelPublicInfo,
  ProviderInfo,
  WhitelistEntry,
} from '@/types/api'
import { http } from '@/api/http'

// ---------------- 上游供应商 ----------------

export function listProviders () {
  return http.get<ListResult<ProviderInfo>>('/admin/catalog/provider/list')
}

export interface ProviderPayload {
  code: string
  name: string
  base_url: string
  /** 明文传入，服务端加密后入库；更新时留空表示不改 */
  api_key?: string
  timeout_seconds?: number
  remark?: string
}

export function createProvider (payload: ProviderPayload) {
  return http.post<{ id: number }>('/admin/catalog/provider/create', payload)
}

export function updateProvider (id: number, payload: Partial<ProviderPayload> & { status?: number }) {
  return http.post<Record<string, never>>(`/admin/catalog/provider/update/${id}`, payload)
}

// ---------------- 模型 ----------------

export function listModels (providerId?: number) {
  return http.get<ListResult<ModelAdminInfo>>('/admin/catalog/model/list', {
    provider_id: providerId,
  })
}

export interface ModelPayload {
  provider_id: number
  model_name: string
  display_name: string
  alias?: string
  supports_stream?: boolean
  supports_multimodal?: boolean
  max_context_tokens?: number
  /** 报表估算费用用；留空表示未配置，报表显示「—」而不是 0 */
  billing_mode?: string
  input_price_per_1m?: number
  output_price_per_1m?: number
  cached_input_price_per_1m?: number
  price_per_call?: number
  currency?: string
  remark?: string
}

export function createModel (payload: ModelPayload) {
  return http.post<{ id: number }>('/admin/catalog/model/create', payload)
}

/**
 * 修改模型
 *
 * 单价与**别名**支持传 null：后端靠「字段在不在请求体里」区分「清空」与「不改动」，
 * 所以想清空就必须显式带 null，不能省略（省略写成 undefined 时 JSON.stringify
 * 会把整个键丢掉，后端当成「没传」，界面却弹「已更新」——别名清不掉就是这样）。
 */
export function updateModel (
  id: number,
  payload: Partial<Omit<ModelPayload, 'provider_id' | 'model_name' | 'alias' | 'input_price_per_1m' | 'output_price_per_1m'
  | 'cached_input_price_per_1m' | 'price_per_call'>> & {
    status?: number
    alias?: string | null
    input_price_per_1m?: number | null
    output_price_per_1m?: number | null
    cached_input_price_per_1m?: number | null
    price_per_call?: number | null
  },
) {
  return http.post<Record<string, never>>(`/admin/catalog/model/update/${id}`, payload)
}

/** 停用模型并清理其全部白名单授权 */
export function disableModel (id: number) {
  return http.post<Record<string, never>>(`/admin/catalog/model/delete/${id}`)
}

// ---------------- 白名单 ----------------

export function listWhitelist (scope?: string, scopeValue?: string) {
  return http.get<ListResult<WhitelistEntry>>('/admin/catalog/whitelist/list', {
    scope,
    scope_value: scopeValue,
  })
}

export interface WhitelistPayload {
  scope: string
  scope_value: string
  model_id: number
  /** false 为显式拒绝，优先级高于所有允许规则 */
  allowed: boolean
  remark?: string
}

export function createWhitelistEntry (payload: WhitelistPayload) {
  return http.post<{ id: number }>('/admin/catalog/whitelist/create', payload)
}

export function updateWhitelistEntry (id: number, payload: { allowed?: boolean, remark?: string }) {
  return http.post<Record<string, never>>(`/admin/catalog/whitelist/update/${id}`, payload)
}

export function deleteWhitelistEntry (id: number) {
  return http.post<Record<string, never>>(`/admin/catalog/whitelist/delete/${id}`)
}

// ---------------- 批量授权与视图（2026-10-19）----------------

export interface WhitelistTarget {
  scope: string
  scope_value: string
}

export interface WhitelistBatchPayload {
  model_ids: number[]
  targets: WhitelistTarget[]
  allowed: boolean
  remark?: string
}

export interface WhitelistBatchResult {
  created: number
  /** 已存在、未改动的条目；existing_allowed=false 说明那条原本是显式拒绝 */
  skipped: (WhitelistTarget & { model_id: number, existing_allowed: boolean })[]
}

/** 多个模型 × 多个部门/角色/成员一次授权。已存在的跳过，不覆盖 */
export function batchCreateWhitelist (payload: WhitelistBatchPayload) {
  return http.post<WhitelistBatchResult>('/admin/catalog/whitelist/batch-create', payload)
}

export function batchDeleteWhitelist (ids: number[]) {
  return http.post<{ deleted: number }>('/admin/catalog/whitelist/batch-delete', { ids })
}

export interface GrantItem {
  scope_value: string
  label: string
  allowed: boolean
  /** 仅部门：这个部门有多少下级部门一起继承了这条授权 */
  descendant_dept_count?: number
}

export interface ModelGrantOverview {
  model_id: number
  display_name: string
  public_name: string
  status: number
  grants: Record<'global' | 'dept' | 'role' | 'user', GrantItem[]>
  rule_count: number
  /** 实际能用这个模型的在职成员数（已算上部门继承与显式拒绝） */
  effective_user_count: number
}

export function whitelistOverview () {
  return http.get<{ items: ModelGrantOverview[], active_user_count: number }>(
    '/admin/catalog/whitelist/overview',
  )
}

export interface AccessSource {
  scope: string
  scope_value: string
  label: string
  allowed: boolean
  /** dept 规则命中的是上级部门时，为那个上级部门的 ID */
  inherited_from_dept_id: number | null
}

export interface MemberModelAccess {
  model_id: number
  display_name: string
  public_name: string | null
  model_status: number | null
  allowed: boolean
  allow_sources: AccessSource[]
  deny_sources: AccessSource[]
}

export interface MemberAccessReport {
  member: { uid: string, name: string, role: string, status: number, dept_id: number | null, dept_path: string }
  items: MemberModelAccess[]
}

/** 按成员反查：每个相关模型能不能用、来自哪条规则 */
export function memberModelAccess (uid: string) {
  return http.get<MemberAccessReport>(`/admin/catalog/whitelist/member-access/${uid}`)
}

// ---------------- 预览 ----------------

/** 某个用户最终可用的模型 —— 配完白名单后自查 */
export function previewUserModels (uid: string) {
  return http.get<{
    user: { uid: string, username: string, permission: string, dept_id: number | null }
    items: ModelPublicInfo[]
    image_items?: { id: number, name: string, display_name: string }[]
  }>(`/client/models/preview/${uid}`)
}
