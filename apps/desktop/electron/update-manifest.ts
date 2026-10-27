/**
 * 更新清单验签 —— 网关那份实现的第三份镜像
 *
 * 另外两份：
 *   audit-gateway/server/services/release_manifest.py   网关：发布时验签
 *   hermes_cli/update_manifest.py                        Python 侧：只读镜像
 *
 * ## 为什么这里非得再写一份
 *
 * 下载并交接安装器的是 Electron 主进程。让它去调 Python sidecar 验签，等于把
 * 「能不能更新」压在「sidecar 是不是健康」上面 —— 而 sidecar 正是更新要修的
 * 东西之一。sidecar 坏掉的那台机器恰好是最需要更新的那台。
 *
 * ## 代价与挡它的办法
 *
 * `canonicalManifestBytes()` 必须与另外两份**逐字节一致**。差一个空格的后果是
 * 全量客户端同时验签失败，且现象是「签名验证不通过」，第一反应会去查密钥而不是
 * 查序列化。三份实现之间没有 import 关系，唯一的防线是
 * `audit-gateway/release/manifest-vectors.json` 这份固定向量 —— 三边测试各读一遍。
 * 改本文件里任何影响字节的东西，另外两份必须同步改。
 *
 * 这里刻意没有签名函数。客户端只有公钥。
 */

import crypto from 'node:crypto'

/** 签名域分隔：防止别处签出来的 JSON 被当成清单签名重放。 */
export const SIGNING_DOMAIN = 'llano-client-update-manifest-v1'

export const SCHEMA_VERSION = 1

export type UpdatePolicy = 'optional' | 'required'

const PLATFORMS = new Set(['darwin', 'win32'])
const ARCHES = new Set(['x64', 'arm64'])
const POLICIES = new Set<UpdatePolicy>(['optional', 'required'])

const VERSION_RE = /^\d+(?:\.\d+){0,3}$/
const SHA256_RE = /^[0-9a-f]{64}$/
const RELEASED_AT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/

const RELEASE_NOTES_MAX_CHARS = 4000
const ARTIFACT_SIZE_MAX = 4 * 1024 * 1024 * 1024

export interface UpdateManifest {
  arch: string
  artifact_sha256: string
  artifact_size: number
  artifact_url: string
  min_supported_version: string
  platform: string
  policy: UpdatePolicy
  release_notes: string
  released_at: string
  schema: number
  version: string
}

export class ManifestError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ManifestError'
  }
}

function requiredText(raw: Record<string, unknown>, key: string): string {
  const value = raw[key]

  if (typeof value !== 'string' || !value.trim()) {
    throw new ManifestError(`清单字段 ${key} 缺失或不是非空字符串`)
  }

  return value.trim()
}

/**
 * 版本号 → 可比较的四元组。补齐到 4 段，否则 `1.2` 和 `1.2.0.0` 会比出大小。
 *
 * 语法刻意收窄成纯数字段：不支持 pre-release 后缀是有意的 ——「1.2.0-rc1 和
 * 1.2.0 谁大」判错的后果是客户端以为自己已是最新版而不再更新，属于静默失效。
 */
export function parseVersion(text: unknown): number[] {
  const value = String(text ?? '').trim()

  if (!VERSION_RE.test(value)) {
    throw new ManifestError(`版本号格式不合法：${JSON.stringify(text)}（只接受 1.2 / 1.2.3 这类纯数字段）`)
  }

  const parts = value.split('.').map(part => Number.parseInt(part, 10))

  while (parts.length < 4) {
    parts.push(0)
  }

  return parts
}

export function compareVersions(left: unknown, right: unknown): number {
  const a = parseVersion(left)
  const b = parseVersion(right)

  for (let index = 0; index < 4; index += 1) {
    const delta = a[index] - b[index]

    if (delta !== 0) {
      return Math.sign(delta)
    }
  }

  return 0
}

export function versionAtLeast(current: unknown, floor: unknown): boolean {
  return compareVersions(current, floor) >= 0
}

/**
 * 校验并归一化，返回**只含**清单字段的新对象。
 *
 * 多余字段被丢弃而不是报错：签名覆盖的就是这里返回的这些，将来网关往响应里加
 * 展示用字段时不该让老签名失效。归一化必须幂等，否则验签会随机失败。
 */
export function normalizeManifest(raw: unknown): UpdateManifest {
  if (!raw || typeof raw !== 'object') {
    throw new ManifestError('清单必须是一个对象')
  }

  const input = raw as Record<string, unknown>
  const schema = input.schema === undefined ? SCHEMA_VERSION : input.schema

  if (schema !== SCHEMA_VERSION) {
    throw new ManifestError(`不支持的清单 schema：${JSON.stringify(schema)}`)
  }

  const version = requiredText(input, 'version')
  parseVersion(version)
  const minSupported = requiredText(input, 'min_supported_version')

  if (!versionAtLeast(version, minSupported)) {
    throw new ManifestError(
      `min_supported_version(${minSupported}) 高于 version(${version})，这会让刚装上的新版本立刻又被判为过期`
    )
  }

  const platform = requiredText(input, 'platform').toLowerCase()

  if (!PLATFORMS.has(platform)) {
    throw new ManifestError(`不支持的平台：${JSON.stringify(platform)}`)
  }

  const arch = requiredText(input, 'arch').toLowerCase()

  if (!ARCHES.has(arch)) {
    throw new ManifestError(`不支持的架构：${JSON.stringify(arch)}`)
  }

  const policy = requiredText(input, 'policy').toLowerCase() as UpdatePolicy

  if (!POLICIES.has(policy)) {
    throw new ManifestError(`不支持的更新策略：${JSON.stringify(policy)}`)
  }

  const artifactUrl = requiredText(input, 'artifact_url')

  if (!/^https?:\/\//i.test(artifactUrl)) {
    throw new ManifestError('artifact_url 必须是 http(s) 地址')
  }

  const sha256 = requiredText(input, 'artifact_sha256').toLowerCase()

  if (!SHA256_RE.test(sha256)) {
    throw new ManifestError('artifact_sha256 必须是 64 位小写十六进制')
  }

  const size = input.artifact_size

  if (typeof size !== 'number' || !Number.isInteger(size) || size <= 0) {
    throw new ManifestError('artifact_size 必须是正整数')
  }

  if (size > ARTIFACT_SIZE_MAX) {
    throw new ManifestError('artifact_size 超出上限')
  }

  const releasedAt = requiredText(input, 'released_at')

  if (!RELEASED_AT_RE.test(releasedAt)) {
    throw new ManifestError('released_at 必须形如 2026-08-24T02:30:00Z（UTC）')
  }

  const rawNotes = input.release_notes ?? ''

  if (typeof rawNotes !== 'string') {
    throw new ManifestError('release_notes 必须是字符串')
  }

  const notes = rawNotes.trim()

  if (notes.length > RELEASE_NOTES_MAX_CHARS) {
    throw new ManifestError(`release_notes 超过 ${RELEASE_NOTES_MAX_CHARS} 字`)
  }

  // 字段顺序与 Python 那两份保持一致：按语义排（schema → version → 平台架构
  // → 策略 → 构建物 → 说明），不是按字母。字母序交给
  // canonicalManifestBytes 里的 sort() 去做 —— 那里才是签名字节的定义处。
  //
  // 这里曾经直接写成字母序，结果是 canonical 里的 sort() 变成了空操作：
  // 把它删掉测试全绿。排序这件事必须只有一个地方负责。
   
  return {
    schema: SCHEMA_VERSION,
    version,
    platform,
    arch,
    policy,
    min_supported_version: minSupported,
    artifact_url: artifactUrl,
    artifact_sha256: sha256,
    artifact_size: size,
    release_notes: notes,
    released_at: releasedAt
  }
   
}

/**
 * 待签名字节。**改这里等于让全量已发布签名失效。**
 *
 * 与 Python 侧 `json.dumps(..., sort_keys=True, separators=(",", ":"),
 * ensure_ascii=False)` 逐字节对齐：键按字典序、无任何可选空白、中文按 UTF-8
 * 原样。`JSON.stringify` 默认就不加空白且不转义非 ASCII，只需自己排键。
 */
export function canonicalManifestBytes(manifest: unknown): Buffer {
  const shaped = normalizeManifest(manifest) as unknown as Record<string, unknown>
  const ordered: Record<string, unknown> = {}

  for (const key of Object.keys(shaped).sort()) {
    ordered[key] = shaped[key]
  }

  return Buffer.concat([Buffer.from(`${SIGNING_DOMAIN}\n`, 'utf8'), Buffer.from(JSON.stringify(ordered), 'utf8')])
}

/**
 * 验签。任何异常都收敛成 false —— 校验函数不该有「出错」这第三种结局。
 *
 * 密钥缺失、base64 烂掉、清单字段不合法、签名对不上，对调用方是同一件事：
 * **不能信任这份清单**。
 */
export function verifyManifest(manifest: unknown, signature: unknown, publicKeyB64: unknown): boolean {
  try {
    const raw = Buffer.from(String(publicKeyB64 ?? ''), 'base64')

    if (raw.length !== 32) {
      return false
    }

    const key = crypto.createPublicKey({
      format: 'jwk',
      key: { crv: 'Ed25519', kty: 'OKP', x: raw.toString('base64url') }
    })

    // 不另外检查签名长度：Node 的 crypto.verify 对任何长度不对的签名都返回
    // false（0/1/63/65/200 字节实测均如此），多写一道判断只会互相遮蔽 ——
    // 变异验证里把它删掉测试全绿，那说明它没有承担任何东西。
    return crypto.verify(null, canonicalManifestBytes(manifest), key, Buffer.from(String(signature ?? ''), 'base64'))
  } catch {
    return false
  }
}

/** 这份清单是否是「这台机器应当安装的更新」。同版本不算；平台架构不符一律不算。 */
export function manifestAppliesTo(
  manifest: unknown,
  options: { arch: string; currentVersion: string; platform: string }
): boolean {
  try {
    const shaped = normalizeManifest(manifest)

    if (shaped.platform !== String(options.platform).trim().toLowerCase()) {
      return false
    }

    if (shaped.arch !== String(options.arch).trim().toLowerCase()) {
      return false
    }

    return compareVersions(options.currentVersion, shaped.version) < 0
  } catch {
    return false
  }
}

/**
 * 是否必须强制阻断业务（U4 全屏遮罩的判定入口）。
 *
 * 两个来源任一成立即强制：清单策略写死 required，或当前版本低于最低可用版本。
 * 后者的存在是为了能对**已经发出去的旧版本**追加强制，而不必重发一版。
 */
export function updateIsMandatory(manifest: unknown, currentVersion: string): boolean {
  const shaped = normalizeManifest(manifest)

  if (shaped.policy === 'required') {
    return true
  }

  return !versionAtLeast(currentVersion, shaped.min_supported_version)
}

export function publicKeyFingerprint(publicKeyB64: unknown): string | null {
  const raw = Buffer.from(String(publicKeyB64 ?? ''), 'base64')

  if (raw.length !== 32) {
    return null
  }

  return crypto.createHash('sha256').update(raw).digest('hex').slice(0, 16)
}
