/** 认证相关接口 —— 对应 server/router/admin/user.py */

import type { LoginInfo, UserInfo } from '@/types/api'
import { http } from '@/api/http'

export function login (username: string, password: string) {
  return http.post<LoginInfo>('/admin/user/login', { username, password })
}

export function fetchCurrentUser () {
  return http.get<UserInfo>('/admin/user/info')
}

export function logout () {
  return http.post<Record<string, never>>('/admin/user/logout')
}

export function fetchDingtalkConfig () {
  return http.get<{ enabled: boolean }>('/admin/user/dingtalk/config')
}

export function fetchDingtalkStatus () {
  return http.get<{ enabled: boolean, bound: boolean }>('/admin/user/dingtalk/status')
}
