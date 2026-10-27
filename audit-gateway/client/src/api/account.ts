/** 账号管理接口 —— 对应 server/router/admin/account.py */

import type { Account, AccountContext, ListResult } from '@/types/api'
import { http } from '@/api/http'

export interface AccountQuery {
  dept_id?: number
  role?: string
  keyword?: string
  limit?: number
  offset?: number
  sort_by?: string
  sort_order?: 'asc' | 'desc'
}

export function listAccounts (query: AccountQuery = {}) {
  return http.get<ListResult<Account> & { total: number }>('/admin/account/list', {
    ...query,
  })
}

/** 当前操作者的权限上下文：可分配角色、拥有的权限点 */
export function fetchAccountContext () {
  return http.get<AccountContext>('/admin/account/me/context')
}

export interface AccountCreatePayload {
  username: string
  permission: string
  realname?: string
  nickname?: string
  dept_id?: number | null
  /** 留空则后端随机生成，明文只在响应里返回一次 */
  password?: string
  status?: number
}

export function createAccount (payload: AccountCreatePayload) {
  return http.post<{ uid: string, initial_password: string | null }>(
    '/admin/account/create',
    payload,
  )
}

export interface AccountAssignPayload {
  uid: string
  dept_id?: number | null
  permission?: string
  realname?: string
  nickname?: string
}

export function assignAccount (payload: AccountAssignPayload) {
  return http.post<Record<string, never>>('/admin/account/assign', payload)
}

/** 启用/禁用账号（总裁停用开发者也走这里） */
export function setAccountStatus (uid: string, status: number) {
  return http.post<Record<string, never>>('/admin/account/status', { uid, status })
}

export function resetAccountPassword (uid: string, password?: string) {
  return http.post<{ initial_password: string | null }>(
    '/admin/account/reset-password',
    { uid, password },
  )
}

export interface SelfPasswordPayload {
  current_password: string
  new_password: string
  revoke_api_keys?: boolean
}

/**
 * 自助修改密码。
 *
 * 与 resetAccountPassword 是**两件不同的事**，别把它们并成一个：
 * 这个要求当前密码（「修改」），那个不要求（「重置」，只有管理员对下属能做）。
 * 少了当前密码这一环，任何拿到一个有效会话的人都能改掉密码把机主锁在门外 ——
 * 一次会话劫持就变成永久接管。
 */
export function changeOwnPassword (payload: SelfPasswordPayload) {
  return http.post<Record<string, never>>('/admin/account/me/password', payload)
}
