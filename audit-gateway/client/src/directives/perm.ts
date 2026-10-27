/**
 * v-perm 指令 —— 按钮级权限
 *
 *   <v-btn v-perm="'dept:edit'">新建部门</v-btn>        无权限则移除该元素
 *   <v-btn v-perm.disable="'dept:edit'">新建部门</v-btn>  无权限则置灰而非隐藏
 *
 * 注意这是纯展示层控制：元素被移除不代表接口调不通，
 * 真正的拦截在网关的 require_roles。
 */

import type { Directive, DirectiveBinding } from 'vue'
import { type Permission, roleHasPermission } from '@/constants/roles'
import { useAuthStore } from '@/stores/auth'

function allowed (permission: Permission): boolean {
  // 指令在组件外执行，这里现取 store（Pinia 已在 main.ts 中安装）
  const auth = useAuthStore()
  return roleHasPermission(
    auth.user?.permission ?? null,
    permission,
    auth.user?.dept_code ?? null,
    auth.user?.permissions,
  )
}

function apply (el: HTMLElement, binding: DirectiveBinding<Permission>): void {
  if (allowed(binding.value)) {
    return
  }

  if (binding.modifiers.disable) {
    el.setAttribute('disabled', 'disabled')
    el.classList.add('v-btn--disabled')
    el.style.pointerEvents = 'none'
    el.style.opacity = '0.5'
    return
  }

  el.remove()
}

export const permDirective: Directive<HTMLElement, Permission> = {
  mounted: apply,
  updated: apply,
}
