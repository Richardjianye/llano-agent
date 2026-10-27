import { requestRaw, uploadRaw } from '@/api/http'

export interface Site {
  id: string
  title: string
  owner_uid: string
  visibility: 'private' | 'users' | 'depts' | 'public'
  allowed_users: string[]
  allowed_depts: string[]
  status: 'draft' | 'published' | 'withdrawn'
  latest_version: number
  published_version: number | null
  created_at: string
  updated_at: string
  url: string
}

export interface SiteVersion {
  version: number
  source_kind: 'html' | 'zip'
  files: string[]
  bytes_total: number
  created_at: string
  preview_url: string
}

export interface SiteSubjects {
  users: { value: string, title: string }[]
  depts: { value: string, title: string }[]
}

export function searchSiteSubjects (query: string) {
  return requestRaw<SiteSubjects>('/sites/subjects', { query: { q: query } })
}

export function listSites () {
  return requestRaw<Site[]>('/sites')
}

export function listSiteVersions (id: string) {
  return requestRaw<SiteVersion[]>(`/sites/${id}/versions`)
}

export function createSite (title: string, file: File) {
  const form = new FormData()
  form.set('title', title)
  form.set('upload', file)
  return uploadRaw<Site>('/sites', form)
}

export function addSiteVersion (id: string, file: File) {
  const form = new FormData()
  form.set('upload', file)
  return uploadRaw<SiteVersion>(`/sites/${id}/versions`, form)
}

export function saveSite (site: Site) {
  return requestRaw<Site>(`/sites/${site.id}`, { method: 'PUT', body: {
    title: site.title,
    visibility: site.visibility,
    allowed_users: site.allowed_users,
    allowed_depts: site.allowed_depts,
  } })
}

export function publishSite (id: string, version: number) {
  return requestRaw<Site>(`/sites/${id}/publish`, { method: 'POST', body: { version } })
}

export function withdrawSite (id: string) {
  return requestRaw<Site>(`/sites/${id}/withdraw`, { method: 'POST' })
}
