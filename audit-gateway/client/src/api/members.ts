/** 成员搜索 —— 对应 server/router/admin/member.py，契约见 server/MEMBER_SEARCH_API.md
 *
 * 各配置页的「选人」控件只走这里。以前是一次拉 /admin/account/list?limit=200 再本地
 * 过滤，第 201 个人起根本不在候选里（生产 290 人）。
 */

import { http } from '@/api/http'

export interface MemberHit {
  uid: string
  username: string | null
  realname: string | null
  nickname: string | null
  display_name: string
  role: string
  status: number
  dept_id: number | null
  dept_name: string | null
  dept_path: string
}

export interface MemberPage {
  items: MemberHit[]
  total: number
  limit: number
  offset: number
  has_more: boolean
}

export interface MemberQuery {
  keyword?: string
  deptIds?: number[]
  includeDescendants?: boolean
  roles?: string[]
  status?: 'active' | 'disabled' | 'all'
  uids?: string[]
  limit?: number
  offset?: number
}

/** 拼成接口参数：空值不发，数组转逗号串 */
export function memberSearchParams (query: MemberQuery): Record<string, string | number | boolean> {
  const params: Record<string, string | number | boolean> = {}
  const keyword = query.keyword?.trim()
  if (keyword) {
    params.keyword = keyword
  }
  if (query.deptIds?.length) {
    params.dept_ids = query.deptIds.join(',')
    params.include_descendants = query.includeDescendants ?? true
  }
  if (query.roles?.length) {
    params.roles = query.roles.join(',')
  }
  if (query.status && query.status !== 'active') {
    params.status = query.status
  }
  if (query.uids?.length) {
    params.uids = [...new Set(query.uids)].join(',')
  }
  params.limit = query.limit ?? 20
  if (query.offset) {
    params.offset = query.offset
  }
  return params
}

export function searchMembers (query: MemberQuery = {}) {
  return http.get<MemberPage>('/admin/member/search', memberSearchParams(query))
}
