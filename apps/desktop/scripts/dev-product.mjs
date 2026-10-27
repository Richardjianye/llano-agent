#!/usr/bin/env node

import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '../../..')
const npmCommand = process.platform === 'win32' ? 'npm.cmd' : 'npm'

let desktopProcess = null
let stopping = false

function shutdown(code = 0) {
  if (stopping) {
    return
  }
  stopping = true

  if (desktopProcess?.exitCode === null) {
    desktopProcess.kill('SIGTERM')
  }

  setTimeout(() => process.exit(code), 250)
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => shutdown(0))
}

console.log('[product-dev] starting Electron; Electron will start and supervise the local Hermes gateway')
console.log('[product-dev] Audit Gateway is external and must be started separately when login/model access is needed')

desktopProcess = spawn(npmCommand, ['run', 'dev:desktop', '--workspace', 'apps/desktop'], {
  cwd: repoRoot,
  env: process.env,
  stdio: 'inherit'
})

desktopProcess.once('error', error => {
  console.error(`[product-dev] failed to start Electron: ${error.message}`)
  shutdown(1)
})
desktopProcess.once('exit', code => shutdown(code ?? 0))
