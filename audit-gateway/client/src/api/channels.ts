import type { ListResult, ProviderInfo } from '@/types/api'
import { http } from '@/api/http'

export interface DiscoveredModel {
  id: string
  supported: boolean
  title: string
  protocol: string
  resolution: string
  max_reference_images: number
}
export interface CodexChannel extends ProviderInfo {
  credential_ok: boolean
  credential_error?: string
  email?: string
  plan_type?: string
  access_expires_at?: number | null
  access_fresh?: boolean
  health?: { state: string, reason: string, checked_at: number } | null
  model_count: number
}
export interface ImageResult {
  model: string
  data: { b64_json: string, mime_type: string }[]
  usage?: Record<string, unknown>
}
export const probeImages = (id: number) => http.post<{ models: DiscoveredModel[] }>(`/admin/images/provider/${id}/probe`)
export function testImage (body: { model_id: number, prompt: string, size: string, reference_images: string[] }) {
  return http.post<ImageResult>('/admin/images/test', body)
}
export const listCodexChannels = () => http.get<ListResult<CodexChannel>>('/admin/codex/providers')
export const startCodexLogin = () => http.post<{ session_id: string, auth_url: string }>('/admin/codex/oauth/start', {})
export function exchangeCodexLogin (body: { session_id: string, code: string, provider_code: string, name: string }) {
  return http.post<{ provider_id: number }>('/admin/codex/oauth/exchange', body)
}
export function importCodexToken (body: { provider_code: string, name: string, refresh_token: string }) {
  return http.post<{ provider_id: number }>('/admin/codex/provider/import', body)
}
export const probeCodex = (id: number) => http.post<{ models: string[] }>(`/admin/codex/provider/${id}/probe`)
export const codexUsage = (id: number) => http.get<{ usage: CodexUsage }>(`/admin/codex/provider/${id}/usage`)
export interface CodexWindow {
  used_percent?: number | null
  reset_at?: number | null
  window_seconds?: number | null
}
export interface CodexUsage {
  [key: string]: unknown
  plan_type?: string
  primary_window?: CodexWindow | null
  secondary_window?: CodexWindow | null
  allowed?: boolean
  limit_reached?: boolean
  reset_credits?: { available_count?: number, applicable_available_count?: number } | null
}
