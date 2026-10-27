/**
 * before-pack.mjs — electron-builder beforePack hook.
 *
 * Two responsibilities:
 *
 * 1. Removes any stale unpacked app directory (`appOutDir`) before
 *    electron-builder stages the Electron binaries into it.
 *
 * WHY THIS EXISTS
 * ---------------
 * electron-builder's final packaging step copies the stock `electron`
 * binary into `release/<platform>-unpacked/` and then renames it to the
 * product name (`Hermes`). If a PREVIOUS `npm run pack` was interrupted
 * (Ctrl-C, OOM kill, crash, full disk) the unpacked directory is left in a
 * corrupted partial state: it keeps the already-renamed `LICENSE.electron.txt`
 * and the Chromium payload (.pak/.so/icudtl.dat/chrome-sandbox) but is MISSING
 * the `electron` binary itself.
 *
 * On the next run, electron-builder sees the destination directory already
 * populated, skips re-copying the binary it thinks is present, then tries to
 * rename a `electron` file that no longer exists. The build dies with:
 *
 *   ENOENT: no such file or directory, rename
 *   '.../release/linux-unpacked/electron' -> '.../release/linux-unpacked/Hermes'
 *
 * This is a hard failure with no obvious cause for the user — `hermes desktop`
 * just prints "Desktop GUI build failed" and the only fix is to manually
 * `rm -rf` the release directory, which a normal user has no way to know.
 *
 * The packaging step is not idempotent across an interrupted run, so we make
 * it idempotent ourselves: wipe the target unpacked directory up front so
 * electron-builder always stages into a clean tree. This is safe — the
 * directory is a pure build artifact that electron-builder fully recreates
 * on every pack; nothing else depends on its prior contents.
 *
 * Cross-platform: the same partial-state trap exists on macOS
 * (the mac-unpacked Hermes.app bundle) and Windows (win-unpacked), so we
 * clean whatever `appOutDir` electron-builder hands us regardless of platform.
 *
 * Best-effort: a cleanup failure must never mask the real build. We log and
 * resolve rather than throw — worst case electron-builder hits the original
 * ENOENT, which is no worse than not having this hook at all.
 *
 * 2. Re-stages node-pty's native files for the ACTUAL target platform/arch
 *    of this pack. `npm run build` already staged node-pty once for the
 *    host machine (see scripts/stage-native-deps.mjs), which is correct for
 *    single-arch builds matching the host. But electron-builder can target
 *    a different arch than the host (cross-build), or pack multiple archs
 *    from one `npm run build` (e.g. `dist:mac` => x64 + arm64). Only this
 *    hook knows the real per-target arch, via `context.arch` /
 *    `context.electronPlatformName` — so it re-stages on top of whatever
 *    `npm run build` left behind, per target, right before files are read
 *    for packing.
 *
 * electron-builder passes a context with:
 *   - appOutDir:            the unpacked app directory about to be staged
 *   - electronPlatformName: 'win32' | 'darwin' | 'linux'
 *   - arch:                 Arch enum (0=ia32, 1=x64, 2=armv7l, 3=arm64, 4=universal)
 */
import { existsSync, readdirSync, rmSync, renameSync, statSync } from 'node:fs'
import path from 'node:path'
import { Arch } from 'electron-builder'
import { stageNodePty, stageGetWindows } from './stage-native-deps.mjs'
import { productExecutableName } from './utils.mjs'
import { SIDECAR_NAME } from './build-sidecar.mjs'
import { desktopRoot, loadProductEnvironment } from './product-environment.mjs'
import { assertSidecarCurrent } from './sidecar-stamp.mjs'
import { assertLocalMcpCurrent, probeLocalMcp } from './stage-local-mcp.mjs'

export function cleanStaleAppOutDir(appOutDir) {
  if (!appOutDir || typeof appOutDir !== 'string') {
    return false
  }
  if (!existsSync(appOutDir)) {
    return false
  }
  // Recursive + force so a half-written tree (read-only bits, partial files)
  // can't block the wipe. retry/maxRetries rides out transient EBUSY on
  // Windows where an AV/indexer may briefly hold a handle.
  rmSync(appOutDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  return true
}

/**
 * Windows rollback material (#69179): before wiping the previous unpacked
 * tree, preserve it as `<appOutDir>.bak` — but ONLY when it holds the product
 * exe (i.e. it is a previously-working build, not the corrupted partial state
 * cleanStaleAppOutDir exists to remove). If the fresh pack then produces a
 * Hermes.exe that Windows can't load (truncated PE from a corrupt cached
 * Electron zip, wrong arch), the updater's integrity gate in
 * `hermes desktop --build-only` (hermes_cli/main.py
 * `_ensure_desktop_exe_launchable`) restores this .bak instead of leaving the
 * user with "This app can't run on your computer".
 *
 * Returns true when the tree was preserved (appOutDir no longer exists), false
 * when there was nothing worth preserving (caller falls through to the wipe).
 * A rename failure (AV holding a handle) also returns false — the wipe is the
 * safe fallback and matches pre-#69179 behavior exactly.
 */
export function preserveRollbackBackup(appOutDir, productExeName = 'Hermes.exe') {
  if (!appOutDir || typeof appOutDir !== 'string' || !existsSync(appOutDir)) {
    return false
  }
  if (!existsSync(path.join(appOutDir, productExeName))) {
    // Partial/corrupt tree (interrupted prior pack) — not rollback material.
    return false
  }
  const backupDir = `${appOutDir}.bak`
  try {
    rmSync(backupDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
    renameSync(appOutDir, backupDir)
    return true
  } catch {
    return false
  }
}

/**
 * 品牌构建必须带上自己的 sidecar，否则装上去只会在登录之后 fail-closed ——
 * 用户拿到一个装得上、跑不起来的包，而构建日志里一个字都没有。这跟
 * `get-windows` 缺原生绑定时硬失败是同一个道理：构建期失败远比运行期失败便宜。
 *
 * `LLANO_ALLOW_SIDECARLESS_BUILD=1` 是给探路包留的口子 —— 阶段 6 一直在打那种
 * 只验安装/卸载链路的包（见 TODO「基线包上机实测」）。**要显式写出来**，
 * 不能默认放行。
 */
function isDirectory(directory) {
  try {
    return statSync(directory).isDirectory()
  } catch {
    return false
  }
}

function containsSkillDefinition(directory) {
  let entries
  try {
    entries = readdirSync(directory, { withFileTypes: true })
  } catch {
    return false
  }

  for (const entry of entries) {
    if (entry.isFile() && entry.name === 'SKILL.md') {
      return true
    }
    if (entry.isDirectory() && containsSkillDefinition(path.join(directory, entry.name))) {
      return true
    }
  }
  return false
}

export function assertSidecarPresent({ allowMissing, brandId, exists = existsSync, sidecarDir }) {
  if (!brandId.trim() || allowMissing) {
    return
  }

  if (!exists(sidecarDir)) {
    throw new Error(
      `[before-pack] 品牌构建缺少 sidecar：${sidecarDir}\n` +
        '  先跑 `npm run build:sidecar`。\n' +
        '  只想打探路包验安装链路的话，显式设 LLANO_ALLOW_SIDECARLESS_BUILD=1。'
    )
  }

  const sidecarRoot = path.dirname(sidecarDir)
  const skillsDir = path.join(sidecarRoot, 'skills')
  const localesDir = path.join(sidecarRoot, 'locales')

  if (!exists(skillsDir) || !isDirectory(skillsDir)) {
    throw new Error(
      `[before-pack] 品牌构建 sidecar 缺少 skills 目录：${skillsDir}\n` +
        '  请完整运行 `npm run build:sidecar`，不要只复制 PyInstaller 产物。'
    )
  }
  if (!containsSkillDefinition(skillsDir)) {
    throw new Error(
      `[before-pack] 品牌构建 sidecar 的 skills 目录不含 SKILL.md：${skillsDir}\n` +
        '  请完整运行 `npm run build:sidecar`，确认内置技能已暂存。'
    )
  }
  if (!exists(localesDir) || !isDirectory(localesDir)) {
    throw new Error(
      `[before-pack] 品牌构建 sidecar 缺少 locales 目录：${localesDir}\n` +
        '  请完整运行 `npm run build:sidecar`，确认语言资源已暂存。'
    )
  }
}

export default async function beforePack(context) {
  const appOutDir = context && context.appOutDir
  const platformName = context && context.electronPlatformName
  const environment = loadProductEnvironment()
  const allowMissing = process.env.LLANO_ALLOW_SIDECARLESS_BUILD === '1'

  // CUSTOM-BEGIN: 品牌发布构建必须带 sidecar（阶段 6 · S6-E5）
  assertSidecarPresent({
    allowMissing,
    brandId: environment.brandId,
    exists: existsSync,
    // ⚠️ 这里曾经是 `path.dirname(new URL(import.meta.url).pathname)`。
    // 在 macOS 上没问题，在 **Windows 上是坏的**：`.pathname` 给出的是
    // `/C:/Users/...`（盘符前多一个斜杠），path.join 之后变成 `\C:\Users\...`，
    // 永远不存在。现象是「sidecar 明明打出来了，打包却说缺 sidecar」，
    // 而报错信息里那个路径乍看完全正常 —— 多出来的那一个字符很难一眼看见。
    //
    // desktopRoot 走的是 fileURLToPath，两个平台都对。
    sidecarDir: path.join(
      desktopRoot,
      'build',
      'sidecar',
      platformName === 'win32' ? `${SIDECAR_NAME}.exe` : SIDECAR_NAME
    )
  })
  if (environment.brandId.trim() && !allowMissing) {
    assertLocalMcpCurrent({ platform: platformName, arch: Arch[context.arch] })
    await probeLocalMcp()
    const sidecarDir = path.join(desktopRoot, 'build', 'sidecar')
    assertSidecarCurrent({
      repoRoot: path.resolve(desktopRoot, '..', '..'), sidecarDir,
      binary: path.join(sidecarDir, platformName === 'win32' ? `${SIDECAR_NAME}.exe` : SIDECAR_NAME),
      platform: platformName, arch: Arch[context.arch]
    })
  }
  // CUSTOM-END: 品牌发布构建必须带 sidecar（阶段 6 · S6-E5）
  try {
    // Windows: keep the previous working build as rollback material for the
    // post-build integrity gate (#69179) instead of destroying it. Falls
    // through to the plain wipe when the old tree is partial/corrupt or the
    // rename fails.
    const productExe = `${(context && context.packager?.appInfo?.productFilename) || productExecutableName()}.exe`
    if (platformName === 'win32' && preserveRollbackBackup(appOutDir, productExe)) {
      console.log(`[before-pack] preserved previous unpacked dir for rollback: ${appOutDir}.bak`)
    } else if (cleanStaleAppOutDir(appOutDir)) {
      console.log(`[before-pack] removed stale unpacked dir before staging: ${appOutDir}`)
    }
  } catch (err) {
    // Never fail the build over cleanup; surface why so a genuinely stuck
    // directory (permissions, mount) is still diagnosable.
    console.warn(`[before-pack] could not clean ${appOutDir} (${err.message}); continuing`)
  }

  try {
    const platform = context && context.electronPlatformName
    const archName = context && typeof context.arch === 'number' ? Arch[context.arch] : undefined
    if (platform && archName) {
      if (archName === 'universal') {
        console.warn(
          '[before-pack] target arch is "universal" — node-pty has no universal prebuild; ' +
            'staged binary will be whichever single-arch copy npm run build left behind. ' +
            'lipo-merge x64/arm64 .node files manually if you need a true universal build.'
        )
      } else {
        await stageNodePty({ platform, arch: archName })
        console.log(`[before-pack] re-staged node-pty for target ${platform}-${archName}`)
      }
      // get-windows' native payload is per-platform, not per-arch (the macOS
      // helper is universal, Windows stages every prebuilt binding dir), so
      // it re-stages for the universal target too.
      // 返回 null 表示没装（品牌构建已移除）。照旧打一行日志，
      // 但别说成「已 stage」—— 构建日志撒谎比不打日志更难查。
      const stagedGetWindows = stageGetWindows({ platform })
      console.log(
        stagedGetWindows
          ? `[before-pack] re-staged get-windows for target ${platform}`
          : '[before-pack] get-windows 未安装，跳过（品牌构建已移除窗口枚举能力）'
      )
    }
  } catch (err) {
    // This one SHOULD fail the build — a missing/wrong native binary for the
    // target arch means a broken package shipped to users, which is worse
    // than a build that fails loudly here.
    throw new Error(`[before-pack] failed to stage native deps for this target: ${err.message}`)
  }
}
