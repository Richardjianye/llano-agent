/**
 * 登录态
 *
 * access_token 只放内存，不写 localStorage —— 后台能看到全员会话审计入口，
 * 令牌被 XSS 摸走的代价太大。刷新页面后靠后端下发的 httpOnly Cookie 恢复会话。
 */

import type { UserInfo } from '@/types/api'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as authApi from '@/api/auth'
import { clearTokens, setTokens, setUnauthorizedHandler } from '@/api/http'
import { roleLabel } from '@/constants/roles'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  /** 会话恢复是否已跑过 —— 路由守卫据此决定要不要等 */
  const initialized = ref(false)
  const loading = ref(false)

  const isLoggedIn = computed(() => user.value !== null)
  const displayName = computed(
    () => user.value?.realname || user.value?.nickname || user.value?.username || '未登录',
  )
  const roleText = computed(() => roleLabel(user.value?.permission))

  async function login (username: string, password: string): Promise<void> {
    const info = await authApi.login(username, password)
    setTokens(info.access_token, info.refresh_token)
    // 登录响应已经带有完整授权上下文，不再依赖第二个 /info 请求才能进入后台。
    user.value = info.user
    initialized.value = true
  }

  /** 刷新页面后恢复会话；未登录时静默失败，不抛错 */
  async function restore (): Promise<void> {
    if (initialized.value) {
      return
    }

    loading.value = true
    try {
      user.value = await authApi.fetchCurrentUser()
    } catch {
      user.value = null
    } finally {
      loading.value = false
      initialized.value = true
    }
  }

  async function logout (): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // 后端登出失败也要清干净本地状态，否则界面停在半登录态
    }
    reset()
  }

  function reset (): void {
    user.value = null
    clearTokens()
    initialized.value = true
  }

  return { user, initialized, loading, isLoggedIn, displayName, roleText, login, restore, logout, reset }
})

/** 在 main.ts 里调用：把「登录态失效」从 http 层接回 store */
export function bindUnauthorizedHandler (onLogout: () => void): void {
  setUnauthorizedHandler(onLogout)
}
