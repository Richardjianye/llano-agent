// Resolve electronDist at runtime (#38673, #47917): electron-builder 26.8.x can
// re-unpack a broken Electron.app; reusing the installed dist dodges that.
// npm workspace hoisting is non-deterministic — require.resolve finds electron
// wherever it landed. Dist present → -c.electronDist=<abs>/dist; absent → let
// electron-builder fetch via @electron/get (electronVersion + ELECTRON_MIRROR).

import fs from "node:fs"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { createRequire } from "node:module"

import { isMain } from "./utils.mjs"
import { selectedProductEnvironment } from "./product-environment.mjs"

const require = createRequire(import.meta.url)

// 环境名要进成品文件名。
//
// development 指向 127.0.0.1、test 指向内网测试服，两个包的**行为完全不同**，
// 但 artifactName 里只有版本号和平台 —— 打完 test 覆盖掉 dev，文件名一模一样。
// 拿到一个 dmg 说不出它连哪个网关，这在小组内测时就是「有人连不上」而现场
// 没有任何线索可查（客户端并不会告诉你它在连谁）。
//
// 显式塞进 env 而不是在 package.json 里写 ${env.LLANO_APP_ENV}：那个变量不设时
// 会展开成空串，文件名里留一个孤零零的连字符，而默认环境恰恰是最常打的那个。
export function artifactEnvSuffix(environment) {
  return String(environment || "").trim() || "development"
}

function electronDistDir() {
  try {
    return path.join(path.dirname(require.resolve("electron/package.json")), "dist")
  } catch {
    return null
  }
}

function distBinary(dist) {
  if (process.platform === "darwin") {
    return path.join(dist, "Electron.app", "Contents", "MacOS", "Electron")
  }
  if (process.platform === "win32") {
    return path.join(dist, "electron.exe")
  }
  return path.join(dist, "electron")
}

// 只有在「构建目标平台 == 本机平台」时才复用本机装着的 electron dist。
//
// 交叉构建时不能复用：`-c.electronDist` 一旦给出去，electron-builder 就不再
// 按目标平台去取运行时，而是直接拿这个目录里的东西装进包里 —— 在 macOS 上
// 打 Windows 包会把 Electron.app 塞进 win-unpacked，成品是个装得上、双击没反应
// 的壳子，而构建全程一个警告都没有。
const PLATFORM_FLAGS = new Map([
  ["--win", "win32"],
  ["-w", "win32"],
  ["--mac", "darwin"],
  ["-m", "darwin"],
  ["--macos", "darwin"],
  ["--linux", "linux"],
  ["-l", "linux"],
])

export function shouldUseLocalElectronDist(argv, hostPlatform) {
  const requested = argv.map(arg => PLATFORM_FLAGS.get(arg)).filter(Boolean)

  // 没点名平台时 electron-builder 打的就是本机平台。
  return requested.every(platform => platform === hostPlatform)
}

function electronBuilderCli() {
  const pkgJson = require.resolve("electron-builder/package.json")
  const bin = require(pkgJson).bin
  const rel = typeof bin === "string" ? bin : bin["electron-builder"]
  return path.join(path.dirname(pkgJson), rel)
}

if (isMain(import.meta.url)) {
  const forwarded = process.argv.slice(2)
  const productEnv = artifactEnvSuffix(selectedProductEnvironment(process.env))
  const crossBuild = !shouldUseLocalElectronDist(forwarded, process.platform)
  const dist = crossBuild ? null : electronDistDir()
  const args = []
  if (dist && fs.existsSync(distBinary(dist))) {
    args.push(`-c.electronDist=${dist}`)
  } else if (crossBuild) {
    console.warn(
      "[run-electron-builder] cross-building off " +
        process.platform +
        "; ignoring the local electron dist and letting @electron/get fetch the target runtime."
    )
  } else {
    console.warn(
      "[run-electron-builder] no local electron dist; electron-builder will fetch " +
        "via @electron/get (electronVersion + ELECTRON_MIRROR)."
    )
  }
  args.push(...forwarded)

  const result = spawnSync(process.execPath, [electronBuilderCli(), ...args], {
    stdio: "inherit",
    env: { ...process.env, LLANO_ARTIFACT_ENV: productEnv },
  })
  if (result.error) {
    console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
    process.exit(1)
  }
  process.exit(result.status == null ? 1 : result.status)
}
