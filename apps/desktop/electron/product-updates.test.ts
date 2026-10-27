import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { applyProductUpdate, checkProductUpdate, rememberVerifiedManifest } from './product-updates'

const vectorsPath = path.resolve(__dirname, '../../../audit-gateway/release/manifest-vectors.json')
const vectors = JSON.parse(fs.readFileSync(vectorsPath, 'utf8'))
const PUBLIC_KEY: string = vectors.test_public_key
const CASE = vectors.cases[0]

const GATEWAY = 'https://gw.example.com'
const MANIFEST_URL = `${GATEWAY}/v1/client-update`

function okJson(body: unknown) {
  return { ok: true, status: 200, text: async () => JSON.stringify(body) } as unknown as Response
}

function okBytes(bytes: Uint8Array) {
  return {
    body: (async function* () {
      yield bytes
    })(),
    ok: true,
    status: 200
  } as unknown as Response
}

const checkBase = {
  arch: 'arm64',
  manifestUrl: MANIFEST_URL,
  platform: 'darwin',
  publicKey: PUBLIC_KEY
}

describe('检查更新', () => {
  beforeEach(() => rememberVerifiedManifest(null))

  it('验签通过时给出版本、策略与强制标记', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(okJson({ update: { manifest: CASE.manifest, signature: CASE.signature } }))

    const status = await checkProductUpdate({ ...checkBase, currentVersion: '1.3.0', fetchImpl })

    expect(status).toMatchObject({
      artifactType: 'installer',
      mandatory: true,
      targetVersion: '1.4.0',
      updateAvailable: true,
      updatePolicy: 'required'
    })
    expect(status.releaseNotes).toContain('修复审计会话折叠')
  })

  it('强制标记是本地从已验签清单算的，不是网关下发的字段', async () => {
    // 网关多塞一个 mandatory:false 也不该影响结论 —— 那是没进签名的东西
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(okJson({ update: { manifest: CASE.manifest, mandatory: false, signature: CASE.signature } }))

    const status = await checkProductUpdate({ ...checkBase, currentVersion: '1.3.0', fetchImpl })

    expect(status.mandatory).toBe(true)
  })

  it('没有更新时不报错也不留下待装清单', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(okJson({ update: null }))

    const status = await checkProductUpdate({ ...checkBase, currentVersion: '1.4.0', fetchImpl })

    expect(status).toMatchObject({ behind: 0, updateAvailable: false })
    expect(status.error).toBeUndefined()
  })

  it('验签失败会把上一次验过的待装清单清掉', async () => {
    // 先成功一次，让 apply 手里真的有东西可装。
    // 少了这一步，下面的断言在"本来就是空的"情况下也成立 —— 变异验证抓到过：
    // 把失败分支里的 rememberVerifiedManifest(null) 删掉，测试照样绿。
    const good = vi.fn().mockResolvedValue(okJson({ update: { manifest: CASE.manifest, signature: CASE.signature } }))

    expect((await checkProductUpdate({ ...checkBase, currentVersion: '1.0.0', fetchImpl: good })).updateAvailable).toBe(
      true
    )

    const tampered = { ...CASE.manifest, artifact_url: `${GATEWAY}/evil.dmg` }
    const bad = vi.fn().mockResolvedValue(okJson({ update: { manifest: tampered, signature: CASE.signature } }))

    const status = await checkProductUpdate({ ...checkBase, currentVersion: '1.0.0', fetchImpl: bad })

    expect(status).toMatchObject({ error: 'bad-signature', updateAvailable: false })

    // 一次失败的检查必须让 apply 无事可做，否则它会去装上一轮那份陈旧清单
    const applied = await applyProductUpdate({
      currentVersion: '1.0.0',
      quitImpl: () => expect.unreachable('不该退出'),
      stagingDir: '/tmp/llano-unused'
    })

    expect(applied).toMatchObject({ code: 'no-verified-update', ok: false })
  })
})

describe('应用更新', () => {
  let stagingDir: string

  beforeEach(() => {
    stagingDir = fs.mkdtempSync(path.join(os.tmpdir(), 'llano-apply-'))
    rememberVerifiedManifest(null)
  })

  afterEach(() => fs.rmSync(stagingDir, { force: true, recursive: true }))

  async function stageVerifiedManifest(payload: Uint8Array) {
    const manifest = {
      ...CASE.manifest,
      artifact_sha256: crypto.createHash('sha256').update(payload).digest('hex'),
      artifact_size: payload.length,
      artifact_url: `${GATEWAY}/downloads/app.dmg`
    }

    // 走真实的检查路径而不是直接塞进去：要证明的是"检查时验过签的那一份"
    // 会被 apply 用到，而不是"塞什么就装什么"
    const signature = crypto
      .sign(
        null,
        Buffer.concat([
          Buffer.from(`${vectors.signing_domain}\n`, 'utf8'),
          Buffer.from(
            JSON.stringify(
              Object.fromEntries(
                Object.entries({ ...manifest, schema: 1 }).sort(([a], [b]) => (a < b ? -1 : 1))
              )
            ),
            'utf8'
          )
        ]),
        crypto.createPrivateKey({
          format: 'jwk',
          key: {
            crv: 'Ed25519',
            d: Buffer.from(vectors.test_private_key, 'base64').toString('base64url'),
            kty: 'OKP',
            x: Buffer.from(PUBLIC_KEY, 'base64').toString('base64url')
          }
        })
      )
      .toString('base64')

    const status = await checkProductUpdate({
      ...checkBase,
      currentVersion: '1.0.0',
      fetchImpl: vi.fn().mockResolvedValue(okJson({ update: { manifest, signature } }))
    })

    expect(status.updateAvailable).toBe(true)

    return manifest
  }

  it('下载校验通过后交接给安装器', async () => {
    const payload = crypto.randomBytes(2048)

    await stageVerifiedManifest(payload)

    const spawnImpl = vi.fn((_command: string, _args: string[]) => ({ unref: vi.fn() }))
    const quitImpl = vi.fn()

    const result = await applyProductUpdate({
      currentVersion: '1.0.0',
      fetchImpl: vi.fn().mockResolvedValue(okBytes(payload)),
      gatewayOrigin: GATEWAY,
      platform: 'darwin',
      quitImpl,
      spawnImpl: spawnImpl as never,
      stagingDir
    })

    expect(result).toMatchObject({ handedOff: true, ok: true, version: '1.4.0' })
    // 交给安装器的必须是暂存目录里那份校验过的文件
    expect(spawnImpl.mock.calls[0]?.[1]?.[0]).toBe(path.join(stagingDir, 'llano-1.4.0-darwin-arm64.dmg'))
    expect(quitImpl).toHaveBeenCalledTimes(1)
  })

  it('哈希不符时不交接、不退出、保留当前版本', async () => {
    const payload = crypto.randomBytes(2048)

    await stageVerifiedManifest(payload)

    const spawnImpl = vi.fn((_command: string, _args: string[]) => ({ unref: vi.fn() }))
    const quitImpl = vi.fn()

    const result = await applyProductUpdate({
      currentVersion: '1.0.0',
      // 托管那一侧返回了别的字节：清单是真的，内容不是
      fetchImpl: vi.fn().mockResolvedValue(okBytes(crypto.randomBytes(2048))),
      gatewayOrigin: GATEWAY,
      platform: 'darwin',
      quitImpl,
      spawnImpl: spawnImpl as never,
      stagingDir
    })

    expect(result).toMatchObject({ code: 'hash-mismatch', handedOff: false, ok: false })
    expect(result.message).toContain('保留当前版本 1.0.0')
    expect(spawnImpl).not.toHaveBeenCalled()
    expect(quitImpl).not.toHaveBeenCalled()
    expect(fs.readdirSync(stagingDir)).toEqual([])
  })

  it('没先检查就直接装 —— 拒绝，且一个字节都不下载', async () => {
    const fetchImpl = vi.fn()

    const result = await applyProductUpdate({
      currentVersion: '1.0.0',
      fetchImpl,
      quitImpl: () => expect.unreachable('不该退出'),
      stagingDir
    })

    expect(result).toMatchObject({ code: 'no-verified-update', ok: false })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})
