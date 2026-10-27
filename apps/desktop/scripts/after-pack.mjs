/**
 * after-pack.mjs — electron-builder afterPack hook.
 *
 * Stamps the Hermes icon + identity onto the packed Windows Hermes.exe via
 * rcedit (delegated to set-exe-identity.mjs). This runs for EVERY packed build
 * — first install, `hermes desktop`, the installer's --update rebuild, and a
 * dev's manual `npm run pack` — so the branded exe can never silently revert
 * to the stock "Electron" icon/name (the bug when the stamp lived only in
 * install.ps1, which the update path doesn't use).
 *
 * Windows-only: rcedit edits PE resources, irrelevant on macOS/Linux where the
 * app identity comes from the bundle Info.plist / desktop entry. Branded Windows builds fail closed if stamping or PE resource verification
 * fails. Stock builds retain the upstream best-effort behavior.
 *
 * electron-builder passes a context with:
 *   - electronPlatformName: 'win32' | 'darwin' | 'linux'
 *   - appOutDir:            the unpacked app directory for this target
 *   - packager.appInfo.productFilename: the exe basename (e.g. 'LlanoAIWorkbench')
 */

import path from 'node:path'
import { readFileSync } from 'node:fs'

import { stampExeIdentity } from './set-exe-identity.mjs'
import { productExecutableName } from './utils.mjs'
import { verifyExeIdentity } from './verify-exe-identity.mjs'
import { loadProductEnvironment } from './product-environment.mjs'
import { assertRunAsNode, assertLocalMcpCurrent, probeLocalMcp } from './stage-local-mcp.mjs'

/** Validate the copied payload with the copied Electron executable, never the build tree. */
export async function verifyPackagedLocalMcp(context, {
  verifyRuntime = assertLocalMcpCurrent,
  verifyFuse = assertRunAsNode,
  probe = probeLocalMcp
} = {}) {
  const name = context.packager?.appInfo?.productFilename || productExecutableName(context.electronPlatformName)
  const bundle = path.join(context.appOutDir, `${name}.app`)
  const executable = context.electronPlatformName === 'darwin'
    ? path.join(bundle, 'Contents', 'MacOS', name)
    : path.join(context.appOutDir, `${name}${context.electronPlatformName === 'win32' ? '.exe' : ''}`)
  const resources = context.electronPlatformName === 'darwin'
    ? path.join(bundle, 'Contents', 'Resources')
    : path.join(context.appOutDir, 'resources')
  const runtimeRoot = path.join(resources, 'local-mcp')
  verifyRuntime({ runtimeRoot })
  await verifyFuse(executable)
  await probe({ runtimeRoot, executable })
}

export async function runAfterPack(context, {
  policy = loadProductEnvironment(),
  stamp = stampExeIdentity,
  verifyIdentity = verifyExeIdentity,
  verifyMcp = verifyPackagedLocalMcp,
  allowSidecarless = process.env.LLANO_ALLOW_SIDECARLESS_BUILD === '1',
  warn = message => console.warn(message)
} = {}) {
  const branded = Boolean(policy.brandId.trim())
  if (context.electronPlatformName === 'win32') {
    const productName = context.packager?.appInfo?.productFilename || productExecutableName('win32')
    const exe = path.join(context.appOutDir, `${productName}.exe`)
    const desktopRoot = path.resolve(import.meta.dirname, '..')
    try {
      // Edit before executing probes: probe the final executable bytes and avoid
      // resource-editor races with a still-closing Electron child process.
      await stamp(exe, desktopRoot)
      if (branded) {
        const { version } = JSON.parse(readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))
        verifyIdentity(exe, path.join(desktopRoot, 'assets', 'icon.ico'), policy.productName, version)
      }
    } catch (err) {
      if (branded) throw new Error(`Windows brand identity verification failed: ${err.message}`, { cause: err })
      warn(`[after-pack] exe identity stamp failed (${err.message}); ${productName}.exe keeps the stock Electron icon`)
    }
  }
  if (branded && !allowSidecarless) await verifyMcp(context)
}

export default async function afterPack(context) {
  await runAfterPack(context)
}
