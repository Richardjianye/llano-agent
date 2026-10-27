/**
 * 权限判定组合式函数
 *
 * 用法：
 *   const { can, isPresident } = usePermission()
 *   <v-btn v-if="can('dept:edit')">新建部门</v-btn>
 *
 * 按钮级控制也可以用 v-perm 指令：<v-btn v-perm="'dept:edit'">
 */

import { computed } from 'vue'
import {
  canManageRole,
  type Permission,
  ROLE,
  roleHasPermission,
  roleLevel,
} from '@/constants/roles'
import { useAuthStore } from '@/stores/auth'

export function usePermission () {
  const auth = useAuthStore()

  const role = computed(() => auth.user?.permission ?? null)
  const deptCode = computed(() => auth.user?.dept_code ?? null)
  const level = computed(() => roleLevel(role.value))

  /** 是否具备某个权限点（audit:view 按部门判定，这里自动带上） */
  function can (permission: Permission): boolean {
    return roleHasPermission(
      role.value,
      permission,
      deptCode.value,
      auth.user?.permissions,
    )
  }

  /** 是否是指定角色之一 */
  function hasRole (...roles: string[]): boolean {
    return role.value !== null && roles.includes(role.value)
  }

  /** 能否管理某个角色的账号 —— 级别必须严格更高 */
  function canManage (targetRole: string | null | undefined): boolean {
    return canManageRole(role.value, targetRole)
  }

  const isPresident = computed(() => role.value === ROLE.PRESIDENT)
  const isDeveloper = computed(
    () => role.value === ROLE.DEVELOPER || role.value === ROLE.ADMIN,
  )

  return { role, deptCode, level, can, hasRole, canManage, isPresident, isDeveloper }
}
