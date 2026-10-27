import { http } from '@/api/http'

export interface DirectoryAccount {
  uid: string
  username: string
  name: string
  dept_id: number | null
  status?: number
  manageable?: boolean
  bound?: boolean
  account_kind?: string
}
export interface DirectoryRow {
  key: string
  category: string
  member: { user_id: string, subject_id: string, name: string, dept_ids: string[] } | null
  account: DirectoryAccount | null
  candidates: DirectoryAccount[]
  mapped_dept_ids: number[]
  can_bind: boolean
  can_create: boolean
  can_disable: boolean
}
export interface DirectoryPreview {
  snapshot_id: string
  created_at: string
  last_success_at: string | null
  complete: boolean
  scope: { departments?: string[], explicit_user_count?: number }
  errors: string[]
  departments: Array<{ id: string, name: string }>
  accounts: DirectoryAccount[]
  rows: DirectoryRow[]
}
export interface DirectoryAction {
  key: string
  action: 'bind' | 'create' | 'disable'
  uid?: string
  username?: string
  dept_id?: number | null
}
export interface InitialCredential {
  username: string
  initial_password: string
  name: string
  uid: string
}
export interface DirectoryResult {
  results: Array<{ key: string, status: string, uid?: string, message?: string }>
  credentials: InitialCredential[]
}
export function previewDirectory () {
  return http.post<DirectoryPreview>('/admin/account/directory/preview')
}
export function applyDirectory (snapshot_id: string, actions: DirectoryAction[]) {
  return http.post<DirectoryResult>('/admin/account/directory/apply', { snapshot_id, actions })
}

/** CSV 引号与公式前缀都要处理，下载只使用本次响应中的新账号。 */
export function credentialsCsv (rows: InitialCredential[]): string {
  const cell = (value: string) => `"${(/^[=+@\-\t\r]/.test(value) ? `'${value}` : value).replaceAll('"', '""')}"`
  return '\uFEFF' + [['用户名', '初始密码', '姓名', 'UID'], ...rows.map(r => [r.username, r.initial_password, r.name, r.uid])]
    .map(row => row.map(value => cell(value)).join(','))
    .join('\r\n')
}

export function auditCredentialsExport (snapshot_id: string, user_uids: string[]) {
  return http.post('/admin/account/directory/export-audit', { snapshot_id, user_uids })
}
