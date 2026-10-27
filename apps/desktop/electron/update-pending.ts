/**
 * 「已下好、等下次启动再装」的待装记录
 *
 * ## 为什么要落盘一条记录，而不是扫暂存目录
 *
 * 暂存目录里有一个 `llano-0.17.14-win32-x64.exe` 只能说明「下过这个文件」，
 * 说不清它是**哪一次检查**认可的、当时的策略是什么、该不该静默装。把这些写成
 * 一条记录，启动时读的就是意图本身，而不是从文件名反推意图。
 *
 * ## 记录不是信任根，暂存文件本身才是
 *
 * 这条记录躺在用户自己能写的目录里，改它毫无门槛。所以启动时**一定要重算一遍
 * 暂存文件的 SHA-256**，与记录里的哈希比对——而记录里的哈希又必须是下载当时从
 * **已验签清单**里抄来的那一个。少了这次重算，「下载时验过了」就变成了
 * 「下载完到下次启动之间的任意时刻，谁改了这个文件都算数」，而这中间隔着一次
 * 关机，时间窗口是无限长的。
 *
 * 重算 180 MB 大约一秒（SHA-256 在这一代机器上约 1–2 GB/s），换掉这个窗口值得。
 *
 * ## 任何一处对不上都是「当作没有待装」
 *
 * 记录坏了、文件没了、哈希不符、版本已经装上了——处理方式都一样：删掉记录和
 * 暂存文件，照常启动。待装更新失败绝不能变成「应用打不开」，它顶多是
 * 「这次没装成，下次检查再来」。
 */

import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import type { UpdateManifest } from './update-manifest'

/** 记录结构变了就换这个数字，旧记录一律当作不可用（而不是尽力解释）。 */
export const PENDING_SCHEMA = 1

export interface PendingUpdate {
  arch: string
  /** 暂存好的安装包绝对路径 */
  artifactPath: string
  platform: string
  policy: 'optional' | 'required'
  schema: number
  sha256: string
  size: number
  /** 落盘时间（毫秒），只用于日志与「这条记录有多旧」的观感 */
  stagedAt: number
  version: string
}

export function pendingRecordPath(updatesDir: string): string {
  return path.join(updatesDir, 'pending.json')
}

export function writePendingUpdate(updatesDir: string, record: PendingUpdate): void {
  fs.mkdirSync(updatesDir, { recursive: true })

  const file = pendingRecordPath(updatesDir)
  const tmp = `${file}.tmp`

  // 先写临时文件再改名：直接覆写的话，写到一半断电会留下一条解析不了的记录。
  // 那种记录虽然会被当成「没有待装」自愈，但会白白丢掉一次已经下完的更新。
  fs.writeFileSync(tmp, `${JSON.stringify(record, null, 2)}\n`, 'utf8')
  fs.renameSync(tmp, file)
}

export function pendingUpdateFrom(manifest: UpdateManifest, artifactPath: string, now = Date.now): PendingUpdate {
  return {
    arch: manifest.arch,
    artifactPath,
    platform: manifest.platform,
    policy: manifest.policy,
    schema: PENDING_SCHEMA,
    sha256: manifest.artifact_sha256,
    size: manifest.artifact_size,
    stagedAt: now(),
    version: manifest.version
  }
}

/** 读记录。结构不对、版本号对不上、字段缺失一律返回 null（不抛）。 */
export function readPendingUpdate(updatesDir: string): PendingUpdate | null {
  let raw: string

  try {
    raw = fs.readFileSync(pendingRecordPath(updatesDir), 'utf8')
  } catch {
    return null
  }

  let parsed: unknown

  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }

  if (!parsed || typeof parsed !== 'object') {
    return null
  }

  const record = parsed as Partial<PendingUpdate>

  if (record.schema !== PENDING_SCHEMA) {
    return null
  }

  const stringFields = [record.artifactPath, record.version, record.platform, record.arch, record.sha256]

  if (stringFields.some(value => typeof value !== 'string' || !value)) {
    return null
  }

  if (record.policy !== 'optional' && record.policy !== 'required') {
    return null
  }

  if (typeof record.size !== 'number' || !Number.isInteger(record.size) || record.size <= 0) {
    return null
  }

  return {
    arch: record.arch as string,
    artifactPath: record.artifactPath as string,
    platform: record.platform as string,
    policy: record.policy,
    schema: PENDING_SCHEMA,
    sha256: record.sha256 as string,
    size: record.size,
    stagedAt: typeof record.stagedAt === 'number' ? record.stagedAt : 0,
    version: record.version as string
  }
}

/** 删记录，并连暂存文件一起删（可选）。任何一步失败都不抛。 */
export function clearPendingUpdate(updatesDir: string, { removeArtifact = true } = {}): void {
  const record = removeArtifact ? readPendingUpdate(updatesDir) : null

  try {
    fs.rmSync(pendingRecordPath(updatesDir), { force: true })
  } catch {
    void 0
  }

  if (record) {
    try {
      fs.rmSync(record.artifactPath, { force: true })
    } catch {
      void 0
    }
  }
}

export function sha256File(file: string): string {
  const digest = crypto.createHash('sha256')
  const handle = fs.openSync(file, 'r')

  try {
    const buffer = Buffer.alloc(1024 * 1024)

    for (;;) {
      const read = fs.readSync(handle, buffer, 0, buffer.length, null)

      if (read <= 0) {
        break
      }

      digest.update(buffer.subarray(0, read))
    }
  } finally {
    fs.closeSync(handle)
  }

  return digest.digest('hex')
}

export type PendingRejection =
  | 'already-installed'
  | 'hash-mismatch'
  | 'missing-artifact'
  | 'no-pending'
  | 'platform-mismatch'
  | 'size-mismatch'

export interface PendingCheck {
  reason?: PendingRejection
  record?: PendingUpdate
  usable: boolean
}

/**
 * 启动时问一句：这条待装记录现在还能用吗？
 *
 * 能用的条件全部满足才算数：记录在、平台架构对得上、版本比当前新、暂存文件在、
 * 体积一致、**重算的 SHA-256 与记录一致**。任何一条不满足都返回原因，
 * 由调用方决定要不要连记录带文件一起清掉。
 */
export function inspectPendingUpdate(
  updatesDir: string,
  options: {
    arch?: string
    currentVersion: string
    hashImpl?: (file: string) => string
    isNewer: (candidate: string, current: string) => boolean
    platform?: string
  }
): PendingCheck {
  const {
    arch = process.arch,
    currentVersion,
    hashImpl = sha256File,
    isNewer,
    platform = process.platform
  } = options

  const record = readPendingUpdate(updatesDir)

  if (!record) {
    return { usable: false, reason: 'no-pending' }
  }

  if (record.platform !== platform || record.arch !== arch) {
    return { record, usable: false, reason: 'platform-mismatch' }
  }

  // 装完之后这条记录还在（安装器不认识它）。如果当前版本已经追上待装版本，
  // 它就是上一轮的残留 —— 再装一次是把同一个安装器又跑一遍。
  if (!isNewer(record.version, currentVersion)) {
    return { record, usable: false, reason: 'already-installed' }
  }

  let stat: fs.Stats

  try {
    stat = fs.statSync(record.artifactPath)
  } catch {
    return { record, usable: false, reason: 'missing-artifact' }
  }

  if (stat.size !== record.size) {
    return { record, usable: false, reason: 'size-mismatch' }
  }

  if (hashImpl(record.artifactPath) !== record.sha256) {
    return { record, usable: false, reason: 'hash-mismatch' }
  }

  return { record, usable: true }
}
