/** 部门接口 —— 对应 server/router/admin/department.py */

import type { Department, DepartmentMember, ListResult } from '@/types/api'
import { http } from '@/api/http'

export function listDepartments (includeDisabled = false) {
  return http.get<ListResult<Department>>('/admin/department/list', {
    include_disabled: includeDisabled,
  })
}

export interface DepartmentCreatePayload {
  code: string
  name: string
  parent_id?: number | null
  remark?: string | null
}

export function createDepartment (payload: DepartmentCreatePayload) {
  return http.post<{ id: number }>('/admin/department/create', payload)
}

export interface DepartmentUpdatePayload {
  dingtalk_dept_id?: string
  name?: string
  parent_id?: number | null
  status?: number
  remark?: string | null
}

export function updateDepartment (id: number, payload: DepartmentUpdatePayload) {
  return http.post<Record<string, never>>(`/admin/department/update/${id}`, payload)
}

export function listDepartmentMembers (id: number, limit = 100, offset = 0) {
  return http.get<ListResult<DepartmentMember>>(`/admin/department/users/${id}`, {
    limit,
    offset,
  })
}

/** 批量转移成员，to_dept_id 留空表示移出部门 */
export function transferMembers (fromDeptId: number, toDeptId?: number) {
  return http.post<Record<string, never>>('/admin/department/transfer', undefined, {
    from_dept_id: fromDeptId,
    to_dept_id: toDeptId,
  })
}
