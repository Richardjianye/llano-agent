import { realpathSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import Vue from '@vitejs/plugin-vue'
import Fonts from 'unplugin-fonts/vite'
import { defineConfig, loadEnv } from 'vite'
import vueDevTools from 'vite-plugin-vue-devtools'
import Vuetify, { transformAssetUrls } from 'vite-plugin-vuetify'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const envDir = fileURLToPath(new URL('.', import.meta.url))
  const env = loadEnv(mode, envDir, '')
  if (env.VITE_API_BASE?.trim()) {
    throw new Error(
      'VITE_API_BASE 会让认证 Cookie 跨站丢失；请改用仅供 Vite 服务端读取的 GATEWAY_ORIGIN。',
    )
  }
  const gatewayOrigin = env.GATEWAY_ORIGIN?.trim() || 'http://127.0.0.1:7899'
  const gatewayProxy = { target: gatewayOrigin, changeOrigin: true }

  return {
    plugins: [
      vueDevTools(),
      tailwindcss(),
      Vue({
        template: { transformAssetUrls },
      }),
      // https://github.com/vuetifyjs/vuetify-loader/tree/master/packages/vite-plugin#readme
      Vuetify({
        autoImport: true,
        styles: {
          configFile: 'src/styles/settings.scss',
        },
      }),
      Fonts({
        fontsource: {
          families: [
            {
              name: 'Roboto Mono',
              weights: [400, 700],
            },
            {
              name: 'Roboto',
              weights: [100, 300, 400, 500, 700, 900],
              styles: ['normal', 'italic'],
            },
          ],
        },
      }),
    ],
    // 用例跑在 node 环境里，而路由表会 import AdminLayout.vue，它又 import
    // vuetify 组件自带的 .css。那些文件默认被当成外部依赖交给 node 直接加载，
    // node 不认识 .css，报的是「Unknown file extension ".css"」——
    // 看着像用例写错了，其实是没让 vite 处理这个依赖。inline 之后就走 vite 转换。
    test: {
      server: { deps: { inline: ['vuetify'] } },
    },
    define: { 'process.env': {} },
    optimizeDeps: {
      include: ['vuetify/components/*'],
    },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('src', import.meta.url)),
      },
      extensions: ['.js', '.json', '.jsx', '.mjs', '.ts', '.tsx', '.vue'],
    },
    server: {
      port: 3000,
      // 隔离 worktree 可复用依赖；字体资源仍需通过 Vite 的文件访问校验。
      fs: { allow: [envDir, realpathSync(fileURLToPath(new URL('node_modules', import.meta.url)))] },
      // 代理到本地网关，保持同源 —— 否则后端下发的 httpOnly Cookie 跨站不会被带上，
      // 刷新页面就掉登录态。生产环境同源部署或配好 CORS_ALLOW_ORIGINS。
      proxy: {
        '/admin': gatewayProxy,
        '/client': gatewayProxy,
        '/healthz': gatewayProxy,
        '/readyz': gatewayProxy,
        '/static': gatewayProxy,
        '/sites': gatewayProxy,
        '/sso': gatewayProxy,
        '/.well-known': gatewayProxy,
      },
    },
  }
})
