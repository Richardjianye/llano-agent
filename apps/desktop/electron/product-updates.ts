/**
 * 公司更新通道 —— 检查、下载、校验、交接
 *
 * ## 这个文件被整体重写过（阶段 5 · U3）
 *
 * 上一版是阶段 2 建产品网关基线时写的（提交 `79eef83c3`）。它的问题不是不够
 * 完善，是**方向不对**：
 *
 * - 清单格式是 `schema_version` / `channel` / `artifacts[]`，与 U1/U2 定下来的
 *   单目标清单对不上，网关发得出去的东西它认不出来；
 * - `ProductUpdateArtifact.signature` 是可选字段，而且**从头到尾没有被校验过**。
 *   一个不验签的更新通道，等于把「谁能改分发通道谁就能改客户端」原样保留着，
 *   而整套签名体系存在的意义就是消掉这一条。
 *
 * 所以不是在它上面加下载，是换掉它。
 *
 * ## 分工
 *
 *   update-manifest.ts   规范字节与验签（与两份 Python 实现共用固定向量）
 *   update-download.ts   取清单、限长下载、SHA-256、原子暂存
 *   update-handoff.ts    交接给系统安装器（唯一不可逆的一步）
 *   本文件               把上面三段串起来，并翻译成渲染层认识的形状
 */

import { spawn } from 'node:child_process'
import path from 'node:path'

import { PRODUCT_POLICY } from './product-policy'
import {
  downloadAndVerifyArtifact,
  fetchSignedManifest,
  type SignedManifest,
  UpdateVerificationError
} from './update-download'
import { handOffToInstaller, supportsSilentInstall } from './update-handoff'
import { compareVersions, updateIsMandatory } from './update-manifest'
import {
  clearPendingUpdate,
  inspectPendingUpdate,
  type PendingUpdate,
  pendingUpdateFrom,
  readPendingUpdate,
  writePendingUpdate
} from './update-pending'

export interface ProductUpdateStatus {
  artifactType?: 'installer'
  behind: number
  currentVersion: string
  error?: string
  fetchedAt: number
  /** 是否必须强制阻断业务。**从已验签的清单自己算**，不是网关下发的字段。 */
  mandatory?: boolean
  message?: string
  releaseNotes?: string
  supported: boolean
  targetVersion?: string
  updateAvailable: boolean
  updatePolicy?: 'optional' | 'required'
}

export interface CheckProductUpdateOptions {
  arch?: string
  currentVersion: string
  fetchImpl?: typeof fetch
  gatewayOrigin?: string
  manifestUrl?: string
  platform?: string
  publicKey?: string
}

/** 上一次检查拿到的已验签清单。交接时不重新取，避免中途换成另一份。 */
let lastVerified: SignedManifest | null = null

export function rememberVerifiedManifest(value: SignedManifest | null): void {
  lastVerified = value
}

export function verifiedManifest(): SignedManifest | null {
  return lastVerified
}

export async function checkProductUpdate(options: CheckProductUpdateOptions): Promise<ProductUpdateStatus> {
  const {
    arch = process.arch,
    currentVersion,
    fetchImpl,
    manifestUrl = PRODUCT_POLICY.updateManifestUrl,
    platform = process.platform,
    publicKey = PRODUCT_POLICY.updateSigningPublicKey
  } = options

  const base: ProductUpdateStatus = {
    behind: 0,
    currentVersion,
    fetchedAt: Date.now(),
    supported: true,
    updateAvailable: false
  }

  try {
    const signed = await fetchSignedManifest({ arch, currentVersion, fetchImpl, manifestUrl, platform, publicKey })

    if (!signed) {
      rememberVerifiedManifest(null)

      return base
    }

    rememberVerifiedManifest(signed)

    return {
      ...base,
      artifactType: 'installer',
      behind: 1,
      mandatory: updateIsMandatory(signed.manifest, currentVersion),
      releaseNotes: signed.manifest.release_notes || undefined,
      targetVersion: signed.manifest.version,
      updateAvailable: true,
      updatePolicy: signed.manifest.policy
    }
  } catch (error) {
    // 验签失败与网络不通是两回事，但对**能不能更新**这个问题答案相同：不能。
    // 分别之处在于前者必须让人看见，所以 code 原样带出去。
    rememberVerifiedManifest(null)

    return {
      ...base,
      error: error instanceof UpdateVerificationError ? error.code : 'check-failed',
      message: error instanceof Error ? error.message : String(error)
    }
  }
}

export interface ApplyProductUpdateOptions {
  /** 与待装记录比对用；默认取当前进程的架构 */
  arch?: string
  currentVersion: string
  fetchImpl?: typeof fetch
  gatewayOrigin?: string
  onProgress?: (received: number, total: number) => void
  platform?: string
  quitImpl: () => void
  /** 静默安装（仅 Windows 有效）。默认 false = 让安装器显示自己的界面。 */
  silent?: boolean
  spawnImpl?: typeof spawn
  stagingDir: string
  /** 待装记录所在目录，默认为 stagingDir 的上一级 */
  updatesDir?: string
}

/**
 * 下载并安装上一次检查确认过的那一版。
 *
 * 只用 `verifiedManifest()` 里那份，不在这里重新取清单：重新取意味着「检查时
 * 看到的」和「实际安装的」可能是两个不同的版本，用户点的是前者。
 */
export async function applyProductUpdate(options: ApplyProductUpdateOptions) {
  const {
    arch = process.arch,
    currentVersion,
    fetchImpl,
    gatewayOrigin = PRODUCT_POLICY.gatewayOrigin,
    onProgress,
    platform = process.platform,
    quitImpl,
    silent = false,
    spawnImpl = spawn,
    stagingDir,
    updatesDir = path.dirname(stagingDir)
  } = options

  const signed = verifiedManifest()

  if (!signed) {
    return {
      code: 'no-verified-update',
      handedOff: false,
      message: '没有已验签的待装版本，请先检查更新',
      ok: false
    }
  }

  let artifactPath: string

  // 后台已经下好同一版的话直接用，别再拉一遍 180 MB。判据是「待装记录说的
  // 就是这一版，且文件重算哈希仍然对得上」—— 复用的前提是重验，不是文件在。
  const staged = usablePendingUpdate({ arch, currentVersion, platform, updatesDir })

  if (staged && staged.version === signed.manifest.version) {
    onProgress?.(staged.size, staged.size)

    return handOffToInstaller({
      artifactPath: staged.artifactPath,
      manifest: signed.manifest,
      platform,
      quitImpl,
      silent,
      spawnImpl: spawnImpl as never
    })
  }

  try {
    artifactPath = await downloadAndVerifyArtifact({
      allowedArtifactOrigins: PRODUCT_POLICY.artifactOrigins,
      fetchImpl,
      gatewayOrigin,
      manifest: signed.manifest,
      onProgress,
      stagingDir
    })
  } catch (error) {
    // 走到这里旧版本一个字节都没动过
    return {
      code: error instanceof UpdateVerificationError ? error.code : 'download-failed',
      handedOff: false,
      message: `${error instanceof Error ? error.message : String(error)}（已保留当前版本 ${currentVersion}）`,
      ok: false
    }
  }

  return handOffToInstaller({
    artifactPath,
    manifest: signed.manifest,
    platform,
    quitImpl,
    silent,
    spawnImpl: spawnImpl as never
  })
}

// CUSTOM-BEGIN: 静默下载 / 下次启动安装（强制更新）

/**
 * 「这次启动能不能直接把上次下好的那一版装上」。
 *
 * 只是 `inspectPendingUpdate` 的一层包装：把版本比较接上项目里唯一那份实现
 * （`compareVersions`，与清单验签共用），并在不可用时**顺手清干净**——留着一条
 * 用不了的记录，下次启动还会再走一遍同样的失败，而暂存目录里那 180 MB
 * 也就一直占着。
 */
export function usablePendingUpdate(options: {
  arch?: string
  currentVersion: string
  hashImpl?: (file: string) => string
  platform?: string
  updatesDir: string
}): null | PendingUpdate {
  const { arch, currentVersion, hashImpl, platform, updatesDir } = options

  const checked = inspectPendingUpdate(updatesDir, {
    arch,
    currentVersion,
    hashImpl,
    isNewer: (candidate, current) => compareVersions(candidate, current) > 0,
    platform
  })

  if (checked.usable && checked.record) {
    return checked.record
  }

  if (checked.reason && checked.reason !== 'no-pending') {
    // 平台不符时别删文件：那多半是记录被手搬过来的，删掉是多管闲事。
    clearPendingUpdate(updatesDir, { removeArtifact: checked.reason !== 'platform-mismatch' })
  }

  return null
}

export interface StageProductUpdateOptions {
  /** 与待装记录比对用；默认取当前进程的架构 */
  arch?: string
  currentVersion: string
  fetchImpl?: typeof fetch
  gatewayOrigin?: string
  onProgress?: (received: number, total: number) => void
  platform?: string
  stagingDir: string
  updatesDir?: string
}

export interface StageProductUpdateResult {
  alreadyStaged?: boolean
  code?: string
  message: string
  staged: boolean
  version?: string
}

/**
 * 后台把更新下好，但**不装**。
 *
 * 强制更新走这条路：用户还在干活的时候不该被一个安装器打断，但也不该等到他
 * 下次开机才开始下 180 MB。所以下载在后台悄悄做完，安装留到下一次启动
 * （`installPendingUpdate`）——这就是雷神那类软件的做法。
 *
 * 与 `applyProductUpdate` 的区别只有一条：这里不交接安装器，因此**这条路上
 * 没有任何不可逆的动作**。下载失败、哈希不符、磁盘满，最坏结果都只是
 * 「这次没下成」，用户手里的版本一个字节没动，而且他根本不会察觉。
 *
 * 幂等：同一版已经下好且重验通过时直接返回 `alreadyStaged`，不重下。
 */
export async function stageProductUpdate(
  options: StageProductUpdateOptions
): Promise<StageProductUpdateResult> {
  const {
    arch = process.arch,
    currentVersion,
    fetchImpl,
    gatewayOrigin = PRODUCT_POLICY.gatewayOrigin,
    onProgress,
    platform = process.platform,
    stagingDir,
    updatesDir = path.dirname(stagingDir)
  } = options

  const signed = verifiedManifest()

  if (!signed) {
    return { code: 'no-verified-update', message: '没有已验签的待装版本', staged: false }
  }

  const staged = usablePendingUpdate({ arch, currentVersion, platform, updatesDir })

  if (staged && staged.version === signed.manifest.version) {
    return {
      alreadyStaged: true,
      message: `${staged.version} 已在后台下载完成，下次启动时安装`,
      staged: true,
      version: staged.version
    }
  }

  let artifactPath: string

  try {
    artifactPath = await downloadAndVerifyArtifact({
      allowedArtifactOrigins: PRODUCT_POLICY.artifactOrigins,
      fetchImpl,
      gatewayOrigin,
      manifest: signed.manifest,
      onProgress,
      stagingDir
    })
  } catch (error) {
    return {
      code: error instanceof UpdateVerificationError ? error.code : 'download-failed',
      message: error instanceof Error ? error.message : String(error),
      staged: false
    }
  }

  // 记录**最后**写。顺序反过来的话，下载中途失败会留下一条指向 .part 或者
  // 根本不存在的文件的记录 —— 下次启动要靠重验才能发现，白跑一趟。
  writePendingUpdate(updatesDir, pendingUpdateFrom(signed.manifest, artifactPath))

  return {
    message: `${signed.manifest.version} 已在后台下载完成，下次启动时安装`,
    staged: true,
    version: signed.manifest.version
  }
}

export interface InstallPendingResult {
  code?: string
  handedOff: boolean
  message: string
  ok: boolean
  /** 这一版下好了但装不了静默（macOS），应当由界面提示用户点一下 */
  needsUserAction?: boolean
  silent?: boolean
  version?: string
}

/**
 * 启动时把上一轮下好的那一版装上。
 *
 * **在开窗之前调用**：这条路成立的时候进程马上就要被安装器换掉，先把窗口画出来
 * 再关掉它，用户看到的是「打开一下又自己关了」，像崩溃。
 *
 * 没有待装、记录对不上、文件被动过 —— 一律返回 `ok:false` 并照常启动。
 * 更新装不上是小事，应用打不开是大事，这两者的优先级不能倒过来。
 *
 * macOS 上即便有待装也不自动交接：`open <dmg>` 会在用户毫无预期时弹出一个
 * 磁盘映像窗口，而且他还得自己拖 —— 那不是「自动安装」，是「开机弹窗」。
 * 这种情况返回 `needsUserAction`，由界面告诉他「已下好，点一下完成安装」。
 */
export function installPendingUpdate(options: {
  arch?: string
  currentVersion: string
  hashImpl?: (file: string) => string
  platform?: string
  quitImpl: () => void
  spawnImpl?: typeof spawn
  updatesDir: string
}): InstallPendingResult {
  const {
    arch,
    currentVersion,
    hashImpl,
    platform = process.platform,
    quitImpl,
    spawnImpl = spawn,
    updatesDir
  } = options

  const staged = usablePendingUpdate({ arch, currentVersion, hashImpl, platform, updatesDir })

  if (!staged) {
    return { code: 'no-pending-update', handedOff: false, message: '没有待装的更新', ok: false }
  }

  if (!supportsSilentInstall(platform)) {
    return {
      code: 'silent-unsupported',
      handedOff: false,
      message: `${staged.version} 已下载完成，需要手动完成安装`,
      needsUserAction: true,
      ok: false,
      version: staged.version
    }
  }

  const result = handOffToInstaller({
    artifactPath: staged.artifactPath,
    // 交接只用得上这三个字段，而它们都来自当初那份已验签清单（记录是抄过来的，
    // 且刚刚重算哈希核对过）。这里不重新取清单：网络在启动这一刻不一定通，
    // 而「已经下好并验过的东西」不该由网络状况决定装不装得上。
    manifest: { arch: staged.arch, platform: staged.platform, version: staged.version } as never,
    platform,
    quitImpl,
    silent: true,
    spawnImpl: spawnImpl as never
  })

  if (result.ok) {
    // 记录用完就删，**但不删安装包**：安装器这会儿正在读它。
    // 装完之后版本号会追上，下一次 usablePendingUpdate 的 already-installed
    // 分支会把残留的包清掉。
    clearPendingUpdate(updatesDir, { removeArtifact: false })
  }

  return { ...result, version: staged.version }
}

/** 待装状态，给界面用（「已下好 0.17.14，重启即可完成安装」）。 */
export function pendingUpdateStatus(updatesDir: string): null | PendingUpdate {
  return readPendingUpdate(updatesDir)
}

// CUSTOM-END: 静默下载 / 下次启动安装
