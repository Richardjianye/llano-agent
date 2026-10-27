/**
 * main.ts
 *
 * 装配插件并挂载应用
 */

// Composables
import { createApp } from 'vue'

// Plugins
import { registerPlugins } from '@/plugins'
import router from '@/router'
import { bindUnauthorizedHandler, useAuthStore } from '@/stores/auth'

// Components
import App from './App.vue'

// Styles
import 'unfonts.css'
import './styles/tailwind.css'
import './styles/main.scss'

const app = createApp(App)

registerPlugins(app)

// 令牌彻底失效（刷新也救不回来）时，清状态并回登录页
const auth = useAuthStore()
bindUnauthorizedHandler(() => {
  auth.reset()
  const current = router.currentRoute.value
  if (current.name !== 'login') {
    router.push({ name: 'login', query: { redirect: current.fullPath } })
  }
})

app.mount('#app')
