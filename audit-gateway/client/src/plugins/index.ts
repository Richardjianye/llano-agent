/**
 * plugins/index.ts
 *
 * 由 ./src/main.ts 调用
 */

// Types
import type { App } from 'vue'

// Plugins
import { createPinia } from 'pinia'
import { permDirective } from '@/directives/perm'
import router from '@/router'
import vuetify from './vuetify'

export function registerPlugins (app: App) {
  app.use(vuetify)
  // Pinia 必须先于 router 安装：导航守卫里会用到 auth store
  app.use(createPinia())
  app.use(router)
  // 按钮级权限：<v-btn v-perm="'dept:edit'">
  app.directive('perm', permDirective)
}
