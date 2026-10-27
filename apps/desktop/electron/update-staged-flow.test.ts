/**
 * 「后台下好、下次启动再装」这条路的行为
 *
 * 用例围着两件事写：**下载这一段不可逆动作一个都不能有**，以及
 * **装之前必须重验暂存文件**。剩下的（下载成功、哈希不符）在
 * update-download.test.ts 里已经覆盖，这里不重复。
 */

import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  applyProductUpdate,
  installPendingUpdate,
  pendingUpdateStatus,
  rememberVerifiedManifest,
  stageProductUpdate,
  usablePendingUpdate
} from './product-updates'
import { normalizeManifest } from './update-manifest'
import { readPendingUpdate, writePendingUpdate } from './update-pending'

const vectorsPath = path.resolve(__dirname, '../../../audit-gateway/release/manifest-vectors.json')
const vectors = JSON.parse(fs.readFileSync(vectorsPath, 'utf8'))
const BASE = normalizeManifest(vectors.cases[0].manifest)

let root = ''
let stagingDir = ''
let updatesDir = ''

const payload = Buffer.from('a-windows-installer-payload')
const payloadHash = crypto.createHash('sha256').update(payload).digest('hex')

/** 与向量同形状，但指向我们自己造的这份字节，且落在认可的分发点下。 */
function winManifest(version = '1.4.0') {
  return {
    ...BASE,
    arch: 'x64',
    artifact_sha256: payloadHash,
    artifact_size: payload.length,
    artifact_url: `${BASE.artifact_url.replace(/[^/]+$/, '')}llano-${version}-win.exe`,
    platform: 'win32',
    policy: 'required' as const,
    version
  }
}

/** 每次调用都要造一份新的：body 是异步生成器，消费一次就空了
 *  （用 mockResolvedValue 复用同一个对象时，第二次下载会收到 0 字节，
 *   表现为 size-mismatch —— 是测试的锅，不是产品的） */
function okBytes(bytes: Uint8Array) {
  return {
    body: (async function* () {
      yield bytes
    })(),
    ok: true,
    status: 200
  } as unknown as Response
}

function gatewayOriginOf(manifest: { artifact_url: string }) {
  return new URL(manifest.artifact_url).origin
}

function spawnHarness() {
  const calls: { args: string[]; command: string }[] = []
  const quitImpl = vi.fn()
  const spawnImpl = vi.fn((command: string, args: string[]) => {
    calls.push({ args, command })

    return { unref: vi.fn() }
  })

  return { calls, quitImpl, spawnImpl }
}

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'staged-update-'))
  updatesDir = path.join(root, 'updates')
  stagingDir = path.join(updatesDir, 'staging')
  rememberVerifiedManifest(null)
})

afterEach(() => {
  fs.rmSync(root, { force: true, recursive: true })
  rememberVerifiedManifest(null)
})

describe('后台下载（不安装）', () => {
  it('下完写下待装记录，且全程没有任何交接动作', async () => {
    const manifest = winManifest()

    rememberVerifiedManifest({ manifest, signature: 'sig' } as never)

    const result = await stageProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      fetchImpl: vi.fn().mockImplementation(async () => okBytes(payload)) as never,
      gatewayOrigin: gatewayOriginOf(manifest),
      platform: 'win32',
      stagingDir
    })

    expect(result).toMatchObject({ staged: true, version: '1.4.0' })

    const record = readPendingUpdate(updatesDir)

    expect(record).toMatchObject({ policy: 'required', sha256: payloadHash, version: '1.4.0' })
    expect(fs.existsSync(record!.artifactPath)).toBe(true)
  })

  it('同一版已经下好时不重下', async () => {
    const manifest = winManifest()

    rememberVerifiedManifest({ manifest, signature: 'sig' } as never)

    const fetchImpl = vi.fn().mockImplementation(async () => okBytes(payload))

    await stageProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      fetchImpl: fetchImpl as never,
      gatewayOrigin: gatewayOriginOf(manifest),
      platform: 'win32',
      stagingDir
    })

    const again = await stageProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      fetchImpl: fetchImpl as never,
      gatewayOrigin: gatewayOriginOf(manifest),
      platform: 'win32',
      stagingDir
    })

    expect(again).toMatchObject({ alreadyStaged: true, staged: true })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('下载失败只是"这次没下成"：不写记录、不留半截文件', async () => {
    const manifest = winManifest()

    rememberVerifiedManifest({ manifest, signature: 'sig' } as never)

    const result = await stageProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      // 字节数对不上 = 托管那头返回了别的东西
      fetchImpl: vi.fn().mockImplementation(async () => okBytes(Buffer.from('short'))) as never,
      gatewayOrigin: gatewayOriginOf(manifest),
      platform: 'win32',
      stagingDir
    })

    expect(result).toMatchObject({ code: 'size-mismatch', staged: false })
    expect(readPendingUpdate(updatesDir)).toBeNull()
    expect(fs.readdirSync(stagingDir)).toEqual([])
  })

  it('没有已验签清单时什么都不做', async () => {
    const result = await stageProductUpdate({ currentVersion: '1.3.0', stagingDir })

    expect(result).toMatchObject({ code: 'no-verified-update', staged: false })
  })
})

describe('下一次启动时安装', () => {
  async function stageOnce(version = '1.4.0') {
    const manifest = winManifest(version)

    rememberVerifiedManifest({ manifest, signature: 'sig' } as never)

    await stageProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      fetchImpl: vi.fn().mockImplementation(async () => okBytes(payload)) as never,
      gatewayOrigin: gatewayOriginOf(manifest),
      platform: 'win32',
      stagingDir
    })

    return readPendingUpdate(updatesDir)!
  }

  it('Windows：静默交接，装完自动重开，并且把记录清掉', async () => {
    const staged = await stageOnce()
    const { calls, quitImpl, spawnImpl } = spawnHarness()

    const result = installPendingUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      platform: 'win32',
      quitImpl,
      spawnImpl: spawnImpl as never,
      updatesDir
    })

    expect(result).toMatchObject({ handedOff: true, ok: true, silent: true, version: '1.4.0' })
    expect(calls[0]).toMatchObject({ args: ['/S', '--force-run'], command: staged.artifactPath })
    expect(quitImpl).toHaveBeenCalledTimes(1)

    // 记录清掉，但安装包留着 —— 安装器这会儿正在读它
    expect(readPendingUpdate(updatesDir)).toBeNull()
    expect(fs.existsSync(staged.artifactPath)).toBe(true)
  })

  it('macOS：下好了也不自动弹 dmg，改成告诉界面要用户点一下', async () => {
    const staged = await stageOnce()

    // 造一条 mac 的记录（真实场景里 mac 根本不会后台下，这里防的是
    // "哪天有人把 mac 也接上后台下载"时静默地弹出磁盘映像窗口）
    writePendingUpdate(updatesDir, {
      ...staged,
      arch: 'arm64',
      platform: 'darwin'
    })

    const { calls, quitImpl, spawnImpl } = spawnHarness()

    const result = installPendingUpdate({
      arch: 'arm64',
      currentVersion: '1.3.0',
      platform: 'darwin',
      quitImpl,
      spawnImpl: spawnImpl as never,
      updatesDir
    })

    expect(result).toMatchObject({ code: 'silent-unsupported', needsUserAction: true, ok: false })
    expect(calls).toEqual([])
    expect(quitImpl).not.toHaveBeenCalled()
    // 记录要留着：界面还得靠它显示"已下好，点一下完成安装"
    expect(readPendingUpdate(updatesDir)).not.toBeNull()
  })

  it('暂存文件在这次开机前被换掉 → 不装，并且清干净', async () => {
    const staged = await stageOnce()

    fs.writeFileSync(staged.artifactPath, Buffer.alloc(payload.length, 0x41))

    const { calls, quitImpl, spawnImpl } = spawnHarness()

    const result = installPendingUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      platform: 'win32',
      quitImpl,
      spawnImpl: spawnImpl as never,
      updatesDir
    })

    expect(result).toMatchObject({ code: 'no-pending-update', ok: false })
    expect(calls).toEqual([])
    expect(quitImpl).not.toHaveBeenCalled()
    expect(readPendingUpdate(updatesDir)).toBeNull()
    expect(fs.existsSync(staged.artifactPath)).toBe(false)
  })

  it('已经装上了的话，残留记录与安装包一起清掉', async () => {
    const staged = await stageOnce()

    // 上一轮装完了，现在跑的就是 1.4.0
    expect(usablePendingUpdate({ arch: 'x64', currentVersion: '1.4.0', platform: 'win32', updatesDir })).toBeNull()
    expect(fs.existsSync(staged.artifactPath)).toBe(false)
    expect(pendingUpdateStatus(updatesDir)).toBeNull()
  })

  it('没有待装时照常启动（不抛、不退出）', () => {
    const { quitImpl, spawnImpl } = spawnHarness()

    const result = installPendingUpdate({
      currentVersion: '1.3.0',
      platform: 'win32',
      quitImpl,
      spawnImpl: spawnImpl as never,
      updatesDir
    })

    expect(result).toMatchObject({ code: 'no-pending-update', ok: false })
    expect(quitImpl).not.toHaveBeenCalled()
  })
})

describe('用户主动点更新', () => {
  it('后台已经下好同一版时直接装，不再下一遍', async () => {
    const manifest = winManifest()

    rememberVerifiedManifest({ manifest, signature: 'sig' } as never)

    const fetchImpl = vi.fn().mockImplementation(async () => okBytes(payload))

    await stageProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      fetchImpl: fetchImpl as never,
      gatewayOrigin: gatewayOriginOf(manifest),
      platform: 'win32',
      stagingDir
    })

    const { calls, quitImpl, spawnImpl } = spawnHarness()
    const progress: number[] = []

    const result = await applyProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      fetchImpl: fetchImpl as never,
      gatewayOrigin: gatewayOriginOf(manifest),
      onProgress: received => progress.push(received),
      platform: 'win32',
      quitImpl,
      silent: true,
      spawnImpl: spawnImpl as never,
      stagingDir
    })

    expect(result).toMatchObject({ handedOff: true, ok: true, silent: true })
    // 只下过一次 —— 就是后台那次
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    // 进度直接跳到 100%，界面不会卡在 0%
    expect(progress).toEqual([payload.length])
    expect(calls[0].args).toEqual(['/S', '--force-run'])
  })

  it('后台下的是另一个版本时，老老实实重新下', async () => {
    const stale = winManifest('1.3.9')

    rememberVerifiedManifest({ manifest: stale, signature: 'sig' } as never)

    const fetchImpl = vi.fn().mockImplementation(async () => okBytes(payload))

    await stageProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      fetchImpl: fetchImpl as never,
      gatewayOrigin: gatewayOriginOf(stale),
      platform: 'win32',
      stagingDir
    })

    const fresh = winManifest('1.4.0')

    rememberVerifiedManifest({ manifest: fresh, signature: 'sig' } as never)

    const { quitImpl, spawnImpl } = spawnHarness()

    const result = await applyProductUpdate({
      arch: 'x64',
      currentVersion: '1.3.0',
      fetchImpl: fetchImpl as never,
      gatewayOrigin: gatewayOriginOf(fresh),
      platform: 'win32',
      quitImpl,
      silent: true,
      spawnImpl: spawnImpl as never,
      stagingDir
    })

    expect(result).toMatchObject({ handedOff: true, ok: true, version: '1.4.0' })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })
})
