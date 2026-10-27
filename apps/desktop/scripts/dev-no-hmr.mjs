#!/usr/bin/env node
// Launch the desktop renderer with HMR disabled so the React Fast Refresh
// preamble path is skipped. This sidesteps a current Vite 8 / plugin-react 6
// bug where the preamble script is not injected into index.html → renderer
// throws "$RefreshReg$ is not defined" on every TSX module → React tree
// never mounts.
//
// We're not trying to use HMR while profiling typing lag anyway. Hermes desktop
// boots, you type, profiler measures. HMR off is fine.
//
// Usage: node apps/desktop/scripts/dev-no-hmr.mjs
//        (then in another shell, run electron --remote-debugging-port=9222 .)

import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

// fileURLToPath 而不是 `.pathname`：后者在 Windows 上给出 `/C:/...`
// （盘符前多一个斜杠），vite 拿着这个路径找不到配置文件。
// 同样的写法在 before-pack.mjs 里坑过一次，见那里的注释。
const server = await createServer({
  configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
  root: fileURLToPath(new URL('../', import.meta.url)),
  server: { hmr: false, host: '127.0.0.1', port: 5174, strictPort: true }
})
await server.listen()
server.printUrls()
