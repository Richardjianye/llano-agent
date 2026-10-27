/**
 * 路由与导航守卫
 *
 * 页面可见性由 meta.permission 决定，与侧边菜单共用同一份权限表，
 * 不会出现「菜单看得见、点进去被踢」的错位。
 */

import { createRouter, createWebHistory } from 'vue-router'
import { roleHasPermission } from '@/constants/roles'
import { routes } from '@/router/routes'
import { useAuthStore } from '@/stores/auth'

export { routes } from '@/router/routes'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

router.beforeEach(async to => {
  const auth = useAuthStore()

  // 刷新页面后先尝试用 httpOnly Cookie 恢复会话，再决定放不放行
  if (!auth.initialized) {
    await auth.restore()
  }

  const dingtalkFailure = to.name === 'login' && typeof to.query.dingtalk_error === 'string' && to.query.dingtalk_error.length > 0

  if (auth.user?.must_change_password && to.name !== 'change-initial-password' && !dingtalkFailure) {
    return { name: 'change-initial-password' }
  }

  if (to.meta.public) {
    // 已登录还去登录页就直接回首页
    if (to.name === 'login' && auth.isLoggedIn && !dingtalkFailure) {
      const redirect = to.query.redirect
      if (typeof redirect === 'string' && redirect.startsWith('/sso/authorize?')) {
        window.location.assign(redirect)
        return false
      }
      return { name: auth.user?.default_route || 'dashboard' }
    }
    return true
  }

  if (!auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  const permission = to.meta.permission
  if (
    permission
    && !roleHasPermission(
      auth.user?.permission,
      permission,
      auth.user?.dept_code,
      auth.user?.permissions,
    )
  ) {
    return { name: 'forbidden' }
  }

  return true
})

router.afterEach(to => {
  const title = to.meta.title
  document.title = title ? `${title} · LLANO AI 审计网关` : 'LLANO AI 审计网关'
})

export default router
