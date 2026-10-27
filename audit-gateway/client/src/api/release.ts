/** 桌面端版本发布 —— 对应 server/router/admin/release.py */

import type { ClientRelease, ReleaseListResult } from '@/types/api'
import { http } from '@/api/http'

/**
 * 发布用的清单与签名，来自离线签名机
 * （audit-gateway/release/sign_release.py 的输出原样）。
 *
 * 前端不校验清单字段：规则的唯一真源在网关的 normalize_manifest，
 * 这里再抄一遍就有了第二份会漂的规则，而两份不一致时的表现是
 * 「后台说合法、发上去被拒」或者更糟的反过来。
 */
export interface ReleasePublishPayload {
  manifest: Record<string, unknown>
  signature: string
}

export function listReleases (params: { platform?: string, arch?: string } = {}) {
  return http.get<ReleaseListResult>('/admin/release/list', { ...params })
}

export function publishRelease (payload: ReleasePublishPayload) {
  return http.post<{ id: number, manifest: Record<string, unknown> }>(
    '/admin/release/publish', payload,
  )
}

export function setReleaseStatus (id: number, status: number) {
  return http.post<Record<string, never>>(`/admin/release/status/${id}`, { status })
}

export function deleteRelease (id: number) {
  return http.post<Record<string, never>>(`/admin/release/delete/${id}`)
}

export function getReleaseDetail (id: number) {
  return http.get<ClientRelease & {
    manifest: Record<string, unknown>
    signature: string
  }>(`/admin/release/detail/${id}`)
}
