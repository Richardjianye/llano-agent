/**
 * 更新构建物的下载、校验与暂存
 *
 * 顺序是刻意的，**每一步都在为下一步排雷**：
 *
 *   1. 取清单 → 2. 验签 → 3. 判定是否适用 → 4. 校验下载地址在网关下
 *   → 5. 限长下载到 .part → 6. 流式 SHA-256 比对 → 7. 原子改名暂存
 *
 * 验签排在下载之前，是因为下载地址本身来自清单：先信清单才谈得上按它去下载。
 *
 * ## 三道校验分别防什么（别把它们当成同一件事）
 *
 * - **签名**：防「这份清单不是我们发的」。这是真正的信任根。
 * - **下载地址必须落在编译期认可的 origin 列表里**：签名已经覆盖了 artifact_url，
 *   所以这一条**挡不住拿到私钥的人**。它的作用是：① 落实「客户端只访问公司
 *   构建物」这条产品规矩；② 接住发布时的手滑（把地址签成了某个公共 CDN）。
 *   把它说成安全边界是不诚实的。
 *
 *   列表 = 网关自己 + product-environments.json 里的 `artifactOrigins`。之所以
 *   不是「必须与网关同源」，是因为网关和分发是两件事：生产网关在 Zeabur，而
 *   一份 180 MB 的安装包该由内网分发机发，不该让全公司绕一趟公网。
 * - **限长 + SHA-256**：这两条**确实**防得住「构建物托管那一侧被控制」——
 *   清单是真的、签名是真的，但那台机器返回了别的字节。限长还顺带挡住
 *   「返回一个无限流把磁盘写满」。
 *
 * ## 失败一律保留旧版
 *
 * 任何一步失败都只是「没更新成」：半截文件删掉，已装的旧版本一个字节都没动。
 * 真正不可逆的动作只有最后交接给安装器那一下，见 update-handoff.ts。
 */

import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { manifestAppliesTo, normalizeManifest, type UpdateManifest, verifyManifest } from './update-manifest'

/** 清单响应体的上限。清单是个小 JSON，几十 KB 都算离谱。 */
const MANIFEST_MAX_BYTES = 256 * 1024

const MANIFEST_TIMEOUT_MS = 15_000
const DOWNLOAD_TIMEOUT_MS = 30 * 60_000

export interface SignedManifest {
  manifest: UpdateManifest
  signature: string
}

export class UpdateVerificationError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.code = code
    this.name = 'UpdateVerificationError'
  }
}

export interface FetchManifestOptions {
  arch: string
  currentVersion: string
  fetchImpl?: typeof fetch
  manifestUrl: string
  platform: string
  publicKey: string
}

/**
 * 向网关问「有没有我该装的更新」，验签通过才返回。
 *
 * 返回 null = 没有可装的更新（已是最新、平台不匹配、网关没发过版）。
 * 抛 UpdateVerificationError = 有东西但**不可信**，这两种情况必须分开：
 * 前者是常态，后者要让人看见。
 */
export async function fetchSignedManifest(options: FetchManifestOptions): Promise<SignedManifest | null> {
  const { arch, currentVersion, fetchImpl = fetch, manifestUrl, platform, publicKey } = options

  if (!publicKey) {
    // 不知道该信任谁的客户端，唯一安全的行为是拒绝安装任何东西
    throw new UpdateVerificationError('no-trust-root', '本版本缺少更新校验公钥，为安全起见暂不安装更新')
  }

  const url = new URL(manifestUrl)

  url.searchParams.set('platform', platform)
  url.searchParams.set('arch', arch)
  url.searchParams.set('version', currentVersion)

  const response = await fetchImpl(url.toString(), {
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(MANIFEST_TIMEOUT_MS)
  })

  if (!response.ok) {
    throw new UpdateVerificationError('gateway-error', `更新服务器返回 HTTP ${response.status}`)
  }

  const text = await response.text()

  if (text.length > MANIFEST_MAX_BYTES) {
    throw new UpdateVerificationError('manifest-too-large', '更新清单大小异常，为安全起见已跳过')
  }

  const payload = JSON.parse(text) as { update?: { manifest?: unknown; signature?: unknown } | null }
  const update = payload?.update

  if (!update) {
    return null
  }

  if (!verifyManifest(update.manifest, update.signature, publicKey)) {
    throw new UpdateVerificationError('bad-signature', '更新清单校验没有通过，为安全起见没有安装')
  }

  const manifest = normalizeManifest(update.manifest)

  // 验签通过不等于「这份更新是给这台机器的」。网关本该只回适用的那一条，
  // 但客户端不拿网关的判断当结论 —— 装错架构的包比不更新糟得多。
  if (!manifestAppliesTo(manifest, { arch, currentVersion, platform })) {
    return null
  }

  return { manifest, signature: String(update.signature) }
}

/**
 * 构建物地址必须落在**本构建认可的 origin 列表**里。见文件头「三道校验分别防什么」。
 *
 * 网关自己永远在列；`extraOrigins` 是编译期固化的额外分发点（见
 * product-environments.json 的 `artifactOrigins`）。列表由构建决定、
 * 用户改不了，所以「客户端只访问公司构建物」这条产品规矩没有松动 ——
 * 松的只是「公司构建物允许放在哪台机器上」。
 *
 * 比对的是 `URL.origin`，不是字符串前缀：`https://gw.example.com.evil.test`
 * 以网关域名开头，但它是另一个 origin，必须落空。
 */
export function artifactUrlAllowed(
  artifactUrl: string,
  gatewayOrigin: string,
  extraOrigins: readonly string[] = []
): boolean {
  let artifact: URL

  try {
    artifact = new URL(artifactUrl)
  } catch {
    return false
  }

  for (const candidate of [gatewayOrigin, ...extraOrigins]) {
    try {
      // 解析不动的条目当作没写。构建期 optionalArtifactOrigins 已经把这种
      // 值挡住了，所以这里能走到的只有「网关地址本身是坏的」，那时候
      // 整个更新链路早就不通了 —— 不该由这一行来发现。
      if (new URL(candidate).origin === artifact.origin) {
        return true
      }
    } catch {
      continue
    }
  }

  return false
}

export interface DownloadOptions {
  /** 网关之外还认哪些分发点。默认空 = 只认网关同源，与放宽之前一致。 */
  allowedArtifactOrigins?: readonly string[]
  fetchImpl?: typeof fetch
  gatewayOrigin: string
  manifest: UpdateManifest
  onProgress?: (received: number, total: number) => void
  stagingDir: string
}

function artifactFileName(manifest: UpdateManifest): string {
  // 文件名不用 URL 里的最后一段：那是清单里带过来的字符串，虽然进了签名，
  // 但用它拼路径就得处理 `..` 和分隔符。用版本号自己拼一个，语义一样清楚。
  const suffix = manifest.platform === 'win32' ? 'exe' : 'dmg'

  return `llano-${manifest.version}-${manifest.platform}-${manifest.arch}.${suffix}`
}

/**
 * 下载并校验，成功后返回暂存好的文件路径。
 *
 * 先写 `.part` 再改名：改名在同一文件系统内是原子的，所以**暂存目录里出现的
 * 完整文件名，一定是校验通过的那一份**。中途断电最多留下一个 .part。
 */
export async function downloadAndVerifyArtifact(options: DownloadOptions): Promise<string> {
  const {
    allowedArtifactOrigins = [],
    fetchImpl = fetch,
    gatewayOrigin,
    manifest,
    onProgress,
    stagingDir
  } = options

  if (!artifactUrlAllowed(manifest.artifact_url, gatewayOrigin, allowedArtifactOrigins)) {
    // 报错里把认可的 origin 全列出来。少列的话，现象是「地址看着没问题却被拒」，
    // 而真正的差别常常是端口或协议这种一眼扫不出来的地方。
    const allowed = [gatewayOrigin, ...allowedArtifactOrigins].join('、')

    throw new UpdateVerificationError(
      'artifact-off-origin',
      `安装包地址 ${manifest.artifact_url} 不在认可的下载地址（${allowed}）范围内，为安全起见没有下载`
    )
  }

  fs.mkdirSync(stagingDir, { recursive: true })

  const finalPath = path.join(stagingDir, artifactFileName(manifest))
  const partPath = `${finalPath}.part`

  fs.rmSync(partPath, { force: true })

  const response = await fetchImpl(manifest.artifact_url, {
    signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS)
  })

  if (!response.ok) {
    throw new UpdateVerificationError('download-failed', `安装包下载失败：HTTP ${response.status}，请检查网络连接后重试`)
  }

  if (!response.body) {
    throw new UpdateVerificationError('download-failed', '安装包下载失败：服务器没有返回内容，请稍后重试')
  }

  const digest = crypto.createHash('sha256')
  let received = 0

  // 清理必须罩住**下载和校验两段**。这两段最初分成了两个 try：下载循环里
  // 「收到的字节超出清单」那一条抛出来时，绕过了只包住后半段的清理，
  // 于是暂存目录里留下一个 .part。测试当场抓到了这个洞。
  try {
    const handle = fs.openSync(partPath, 'w')

    try {
      for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
        received += chunk.length

        // 严格按清单里的体积卡死，不留余量：多出来的字节没有任何正当来源，
        // 而放任它意味着一个被控制的托管机可以一直写到磁盘满。
        if (received > manifest.artifact_size) {
          throw new UpdateVerificationError(
            'size-mismatch',
            `构建物比清单声明的 ${manifest.artifact_size} 字节更长，已中止下载`
          )
        }

        digest.update(chunk)
        fs.writeSync(handle, chunk)
        onProgress?.(received, manifest.artifact_size)
      }
    } finally {
      fs.closeSync(handle)
    }

    if (received !== manifest.artifact_size) {
      throw new UpdateVerificationError(
        'size-mismatch',
        `构建物只收到 ${received} 字节，清单声明 ${manifest.artifact_size} 字节`
      )
    }

    const actual = digest.digest('hex')

    if (actual !== manifest.artifact_sha256) {
      throw new UpdateVerificationError(
        'hash-mismatch',
        `构建物 SHA-256 不符：期望 ${manifest.artifact_sha256}，实际 ${actual}`
      )
    }
  } catch (error) {
    // 校验没过的半截文件必须删掉：留着它，下一次「文件已存在」的快路径就会
    // 把一份没验过的东西当成验过的
    fs.rmSync(partPath, { force: true })
    throw error
  }

  fs.rmSync(finalPath, { force: true })
  fs.renameSync(partPath, finalPath)

  return finalPath
}
