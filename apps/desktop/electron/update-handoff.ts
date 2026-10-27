/**
 * 把已验签、已校验哈希的安装包交给系统安装器
 *
 * ## 这是整条链路里唯一不可逆的一步
 *
 * 前面每一步失败都只是「没更新成」：半截文件删掉，已装的旧版本一个字节都没动。
 * 到这里才真正把控制权交出去。所以：
 *
 * - **先起安装器，成功了才退出自己**。顺序反过来的话，spawn 失败就变成
 *   「应用关了、更新也没装上」—— 用户手里剩下一个打不开的东西。
 * - 起安装器失败**不退出**，原样返回错误。旧版本还在跑，用户至少能继续干活。
 *
 * ## 「回滚」在 installer 更新里是什么意思
 *
 * 不是我们回滚，是**根本没动**。交接之前失败 = 旧版本完好；交接之后由安装器
 * 自己负责（NSIS 有自己的回滚，macOS 是用户手动拖拽替换）。我们能保证的是
 * 「没验过的东西绝不会被交给安装器」，不是「装坏了能自动退回去」。
 * 文档里不要写成后者。
 *
 * ## 静默安装只有 Windows 有
 *
 * Windows 走 NSIS 的 `/S --force-run`：不弹界面，装完把应用重新拉起来。
 *
 * macOS 这一步只是「打开安装包」：`open <dmg>` 挂载磁盘映像，由用户把应用拖进
 * Applications。这**不是**静默安装，要做到那一步需要 `.pkg` 或者 Squirrel.Mac
 * 那种 zip 原地替换，两者都得先有 Developer ID 签名与公证 —— 当前的包是
 * ad-hoc 签名，所以这件事卡在签名上，不在这个文件里。
 *
 * 因此上层**必须先问 `supportsSilentInstall()`**，而不是传个 silent:true 就
 * 当成静默装好了。在 macOS 上传 silent 会被原样忽略（计划里的 silent 是
 * false），界面该说的还是「点一下完成安装」。
 */

import type { UpdateManifest } from './update-manifest'

export interface HandoffPlan {
  args: string[]
  command: string
  /** 这条计划是不是真的静默（macOS 上永远不是，见文件头） */
  silent: boolean
}

/**
 * 这个平台能不能做到「装完不打扰人」。
 *
 * Windows 能：NSIS 认 `/S`，`--force-run` 让它装完把应用重新拉起来。
 * macOS 不能：`.dmg` 只能挂载让用户自己拖，要静默得换 `.pkg` 或 Squirrel.Mac，
 * 两者都得先有 Developer ID 签名与公证。这里返回 false 而不是假装能做到 ——
 * 上层据此决定是「后台装好」还是「下好了，点一下装」。
 */
export function supportsSilentInstall(platform: string = process.platform): boolean {
  return platform === 'win32'
}

export class HandoffError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.code = code
    this.name = 'HandoffError'
  }
}

export function planInstallerHandoff(
  artifactPath: string,
  platform: string,
  { silent = false }: { silent?: boolean } = {}
): HandoffPlan {
  if (platform === 'darwin') {
    // 挂载磁盘映像，由用户拖进 Applications。silent 在这里只能被忽略，
    // 不能默默降级成"以为装好了"——调用方应当先问 supportsSilentInstall()。
    return { args: [artifactPath], command: 'open', silent: false }
  }

  if (platform === 'win32') {
    // 交互式（默认）：NSIS 显示自己的界面。用户从「关于」点更新、或者
    // 强制遮罩上点安装时，看得见进度、装失败时看得见错误。
    //
    // 静默（silent）：`/S` 关掉界面，`--force-run` 让安装器装完把应用拉起来
    // （assisted 安装器只在 isForceRun && Silent 同时成立时才自动启动，见
    // app-builder-lib/templates/nsis/installSection.nsh）。少了 --force-run
    // 的表现是"应用自己关了再也没回来"，比不静默还糟。
    //
    // 静默只用在两个地方：① 用户在「关于」里主动点的更新；
    // ② 上一次已经在后台下好、这一次启动时接着装的强制更新。
    // 两种情况用户都知道自己在更新，不是"用着用着程序被换掉了"。
    return { args: silent ? ['/S', '--force-run'] : [], command: artifactPath, silent }
  }

  throw new HandoffError('unsupported-platform', `不支持在 ${platform} 上交接安装器`)
}

export interface HandoffOptions {
  artifactPath: string
  manifest: UpdateManifest
  platform?: string
  quitImpl: () => void
  /** 走静默安装（仅 Windows 有效，见 supportsSilentInstall） */
  silent?: boolean
  spawnImpl: (command: string, args: string[], options: { detached: boolean; stdio: 'ignore' }) => { unref: () => void }
}

export interface HandoffResult {
  code?: string
  handedOff: boolean
  message: string
  ok: boolean
  /** 交接出去的是不是静默安装（决定界面该说"装完自动重开"还是"按提示操作"） */
  silent?: boolean
  version?: string
}

export function handOffToInstaller(options: HandoffOptions): HandoffResult {
  const { artifactPath, manifest, platform = process.platform, quitImpl, silent = false, spawnImpl } = options

  let plan: HandoffPlan

  try {
    plan = planInstallerHandoff(artifactPath, platform, { silent })
  } catch (error) {
    return {
      code: error instanceof HandoffError ? error.code : 'handoff-failed',
      handedOff: false,
      message: error instanceof Error ? error.message : String(error),
      ok: false
    }
  }

  try {
    const child = spawnImpl(plan.command, plan.args, { detached: true, stdio: 'ignore' })

    child.unref()
  } catch (error) {
    // 起不来就别退出。退了等于「应用关了、更新也没装上」。
    return {
      code: 'spawn-failed',
      handedOff: false,
      message: `启动安装器失败，已保留当前版本：${error instanceof Error ? error.message : String(error)}`,
      ok: false
    }
  }

  // 两个平台都必须退：Windows 安装器要独占正在运行的文件，macOS 上不退的话
  // 用户会对着一个还在跑的旧版本往 Applications 里拖新版本。
  //
  // 这里曾经有个 plan.quitAfterSpawn 开关，但两个平台都填 true，变异验证证明
  // 把判断去掉测试全绿 —— 它没有区分任何东西。真出现"装完不用退"的平台时
  // 再加回来，连着用例一起。
  quitImpl()

  return {
    handedOff: true,
    message: plan.silent
      ? `正在静默安装 ${manifest.version}，装完会自动重新打开`
      : `已启动 ${manifest.version} 的安装程序`,
    ok: true,
    silent: plan.silent,
    version: manifest.version
  }
}
