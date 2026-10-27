/** 全局提示条 —— 由 App.vue 里的单个 v-snackbar 渲染 */

import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ApiError } from '@/api/http'

type ToastColor = 'success' | 'error' | 'warning' | 'info'

export const useToastStore = defineStore('toast', () => {
  const visible = ref(false)
  const message = ref('')
  const color = ref<ToastColor>('info')

  function show (text: string, kind: ToastColor = 'info'): void {
    message.value = text
    color.value = kind
    visible.value = true
  }

  const success = (text: string) => show(text, 'success')
  const warn = (text: string) => show(text, 'warning')

  /** 统一的报错展示：ApiError 用后端 message，其他异常兜底 */
  function error (err: unknown, fallback = '操作失败'): void {
    if (err instanceof ApiError) {
      show(err.message || fallback, 'error')
      return
    }
    if (err instanceof Error) {
      show(err.message || fallback, 'error')
      return
    }
    show(fallback, 'error')
  }

  return { visible, message, color, show, success, warn, error }
})
