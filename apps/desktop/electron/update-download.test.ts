import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  artifactUrlAllowed,
  downloadAndVerifyArtifact,
  fetchSignedManifest,
  UpdateVerificationError
} from './update-download'
import { normalizeManifest } from './update-manifest'

const vectorsPath = path.resolve(__dirname, '../../../audit-gateway/release/manifest-vectors.json')
const vectors = JSON.parse(fs.readFileSync(vectorsPath, 'utf8'))
const PUBLIC_KEY: string = vectors.test_public_key
const CASE = vectors.cases[0]

const GATEWAY = 'https://gw.example.com'

function manifestResponse(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    text: async () => JSON.stringify(body)
  } as unknown as Response
}

function bodyOf(bytes: Uint8Array, ok = true, status = 200) {
  return {
    body: (async function* () {
      yield bytes
    })(),
    ok,
    status
  } as unknown as Response
}

describe('取清单', () => {
  const base = {
    arch: 'arm64',
    currentVersion: '1.3.0',
    manifestUrl: `${GATEWAY}/v1/client-update`,
    platform: 'darwin',
    publicKey: PUBLIC_KEY
  }

  it('验签通过才返回', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(manifestResponse({ update: { manifest: CASE.manifest, signature: CASE.signature } }))

    const result = await fetchSignedManifest({ ...base, fetchImpl })

    expect(result?.manifest.version).toBe('1.4.0')
    // 平台、架构、当前版本必须作为查询参数带上，否则网关没法判断该回哪一条
    const called = new URL(fetchImpl.mock.calls[0][0])

    expect(called.searchParams.get('platform')).toBe('darwin')
    expect(called.searchParams.get('arch')).toBe('arm64')
    expect(called.searchParams.get('version')).toBe('1.3.0')
  })

  it('没有更新时返回 null', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(manifestResponse({ update: null }))

    await expect(fetchSignedManifest({ ...base, fetchImpl })).resolves.toBeNull()
  })

  it('签名对不上就抛，不是当成没有更新', async () => {
    const tampered = { ...CASE.manifest, artifact_url: 'https://evil.example.com/x.dmg' }
    const fetchImpl = vi.fn().mockResolvedValue(manifestResponse({ update: { manifest: tampered, signature: CASE.signature } }))

    await expect(fetchSignedManifest({ ...base, fetchImpl })).rejects.toMatchObject({ code: 'bad-signature' })
  })

  it('没有内置公钥时拒绝，而且根本不发请求', async () => {
    const fetchImpl = vi.fn()

    await expect(fetchSignedManifest({ ...base, fetchImpl, publicKey: '' })).rejects.toMatchObject({
      code: 'no-trust-root'
    })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('网关报错时抛，不是当成没有更新', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(manifestResponse({}, false, 503))

    await expect(fetchSignedManifest({ ...base, fetchImpl })).rejects.toMatchObject({ code: 'gateway-error' })
  })

  it('清单过大直接拒绝解析', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => 'x'.repeat(300 * 1024)
    } as unknown as Response)

    await expect(fetchSignedManifest({ ...base, fetchImpl })).rejects.toMatchObject({ code: 'manifest-too-large' })
  })

  it('验签过了但平台对不上，仍然不当作可装的更新', async () => {
    // 网关本该只回适用的那一条；客户端不拿它的判断当结论
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(manifestResponse({ update: { manifest: CASE.manifest, signature: CASE.signature } }))

    await expect(fetchSignedManifest({ ...base, arch: 'x64', fetchImpl, platform: 'win32' })).resolves.toBeNull()
  })

  it('已经是更新的版本时不会被推回旧版', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(manifestResponse({ update: { manifest: CASE.manifest, signature: CASE.signature } }))

    await expect(fetchSignedManifest({ ...base, currentVersion: '9.9.9', fetchImpl })).resolves.toBeNull()
  })
})

describe('构建物地址', () => {
  it.each([
    [`${GATEWAY}/downloads/app.dmg`, true],
    // :443 是 https 的默认端口，URL 会把它归一化掉 —— 同源，放行是对的
    [`${GATEWAY}:443/x.dmg`, true],
    // 换成非默认端口就是另一个 origin
    ['https://gw.example.com:8443/x.dmg', false],
    ['https://evil.example.com/app.dmg', false],
    // 后缀撞名：evil.test 底下挂一个以网关域名开头的子域
    ['https://gw.example.com.evil.test/app.dmg', false],
    // 协议不同也是不同 origin，降级到 http 不放行
    ['http://gw.example.com/app.dmg', false],
    ['不是地址', false]
  ])('%s → %s', (url, expected) => {
    expect(artifactUrlAllowed(url, GATEWAY)).toBe(expected)
  })

  // 额外分发点（product-environments.json 的 artifactOrigins）。生产网关在
  // Zeabur，安装包在内网分发机上，两者本来就不同源。
  const MIRROR = 'http://192.0.2.130'

  it.each([
    ['分发点自己', `${MIRROR}/releases/app.dmg`, true],
    // 端口是 origin 的一部分：只放行了 80，8080 就不该跟着一起过
    ['同主机换端口', `${MIRROR}:8080/app.dmg`, false],
    // 协议同理，别名单里写 http 就把 https 也当自己人
    ['同主机换协议', 'https://192.0.2.130/app.dmg', false],
    ['网关自己仍然认', `${GATEWAY}/downloads/app.dmg`, true],
    ['列表外的地址照旧拒绝', 'https://evil.example.com/app.dmg', false]
  ])('%s：%s → %s', (_label, url, expected) => {
    expect(artifactUrlAllowed(url, GATEWAY, [MIRROR])).toBe(expected)
  })

  // 空列表必须与「放宽之前」逐字一致 —— 这条挡的是「默认值悄悄放开了什么」。
  it('列表为空时与只认网关同源完全一致', () => {
    expect(artifactUrlAllowed(`${MIRROR}/app.dmg`, GATEWAY, [])).toBe(false)
    expect(artifactUrlAllowed(`${GATEWAY}/app.dmg`, GATEWAY, [])).toBe(true)
  })

  // 网关地址本身坏掉时不能变成「谁都放行」。空列表 + 坏网关地址是最容易
  // 写成 `return true` 的分支。
  it('网关地址解析不动时一律拒绝，而不是放行', () => {
    expect(artifactUrlAllowed(`${MIRROR}/app.dmg`, '不是地址')).toBe(false)
    expect(artifactUrlAllowed(`${MIRROR}/app.dmg`, '不是地址', ['也不是地址'])).toBe(false)
  })
})

describe('下载与校验', () => {
  let stagingDir: string

  beforeEach(() => {
    stagingDir = fs.mkdtempSync(path.join(os.tmpdir(), 'llano-update-'))
  })

  afterEach(() => {
    fs.rmSync(stagingDir, { force: true, recursive: true })
  })

  function manifestFor(payload: Uint8Array, overrides: Record<string, unknown> = {}) {
    return normalizeManifest({
      ...CASE.manifest,
      artifact_sha256: crypto.createHash('sha256').update(payload).digest('hex'),
      artifact_size: payload.length,
      artifact_url: `${GATEWAY}/downloads/app.dmg`,
      ...overrides
    })
  }

  it('校验通过后落成完整文件，没有残留 .part', async () => {
    const payload = crypto.randomBytes(4096)
    const manifest = manifestFor(payload)

    const staged = await downloadAndVerifyArtifact({
      fetchImpl: vi.fn().mockResolvedValue(bodyOf(payload)),
      gatewayOrigin: GATEWAY,
      manifest,
      stagingDir
    })

    expect(fs.readFileSync(staged)).toEqual(payload)
    expect(fs.readdirSync(stagingDir).filter(name => name.endsWith('.part'))).toEqual([])
  })

  it('哈希不符时拒绝，并且不留下任何文件', async () => {
    const payload = crypto.randomBytes(1024)
    const manifest = manifestFor(payload, { artifact_sha256: 'b'.repeat(64) })

    await expect(
      downloadAndVerifyArtifact({
        fetchImpl: vi.fn().mockResolvedValue(bodyOf(payload)),
        gatewayOrigin: GATEWAY,
        manifest,
        stagingDir
      })
    ).rejects.toMatchObject({ code: 'hash-mismatch' })

    // 留着半截文件，下次「文件已存在」的快路径就会把没验过的当成验过的
    expect(fs.readdirSync(stagingDir)).toEqual([])
  })

  it('返回的字节比清单长就中止，不等下完再算哈希', async () => {
    const payload = crypto.randomBytes(1024)
    const manifest = manifestFor(payload, { artifact_size: 512 })

    await expect(
      downloadAndVerifyArtifact({
        fetchImpl: vi.fn().mockResolvedValue(bodyOf(payload)),
        gatewayOrigin: GATEWAY,
        manifest,
        stagingDir
      })
    ).rejects.toMatchObject({ code: 'size-mismatch' })
    expect(fs.readdirSync(stagingDir)).toEqual([])
  })

  it('超长时**当场中止**，不会把整个流读完', async () => {
    // 这一条和上面那条看起来重复，其实测的是不同的东西。
    //
    // 上面那条只要求"最终报错"——把循环里那道长度闸门删掉，末尾的
    // `received !== artifact_size` 照样会报同一个 code，测试照样绿。
    // 变异验证抓到了这一点。
    //
    // 循环里那道闸门存在的理由是**当场停下**：一个被控制的托管机可以一直往下
    // 写，不当场停就会把磁盘写满。所以这里断言的是"读了几块就停了"。
    const chunk = crypto.randomBytes(1024)
    let pulled = 0
    const manifest = manifestFor(chunk, { artifact_size: 1024 })

    const endless = {
      body: (async function* () {
        for (let index = 0; index < 1000; index += 1) {
          pulled += 1
          yield chunk
        }
      })(),
      ok: true,
      status: 200
    } as unknown as Response

    await expect(
      downloadAndVerifyArtifact({
        fetchImpl: vi.fn().mockResolvedValue(endless),
        gatewayOrigin: GATEWAY,
        manifest,
        stagingDir
      })
    ).rejects.toMatchObject({ code: 'size-mismatch' })

    // 1024 字节的额度，第 2 块就该被拦下
    expect(pulled).toBeLessThanOrEqual(2)
    expect(fs.readdirSync(stagingDir)).toEqual([])
  })

  it('返回的字节比清单短也拒绝', async () => {
    const payload = crypto.randomBytes(1024)
    const manifest = manifestFor(payload, { artifact_size: 2048 })

    await expect(
      downloadAndVerifyArtifact({
        fetchImpl: vi.fn().mockResolvedValue(bodyOf(payload)),
        gatewayOrigin: GATEWAY,
        manifest,
        stagingDir
      })
    ).rejects.toMatchObject({ code: 'size-mismatch' })
  })

  it('不在认可分发点下的构建物根本不发请求', async () => {
    const payload = crypto.randomBytes(64)
    const manifest = manifestFor(payload, { artifact_url: 'https://evil.example.com/app.dmg' })
    const fetchImpl = vi.fn()

    await expect(
      downloadAndVerifyArtifact({ fetchImpl, gatewayOrigin: GATEWAY, manifest, stagingDir })
    ).rejects.toMatchObject({ code: 'artifact-off-origin' })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  // 报错要把认可的分发点列出来。被拒时最常见的真实原因是端口/协议对不上，
  // 而那两样恰恰是肉眼扫地址时最容易看漏的。
  it('拒绝时把认可的分发点写进报错', async () => {
    const manifest = manifestFor(crypto.randomBytes(64), { artifact_url: 'https://evil.example.com/app.dmg' })

    await expect(
      downloadAndVerifyArtifact({
        allowedArtifactOrigins: ['http://192.0.2.130'],
        fetchImpl: vi.fn(),
        gatewayOrigin: GATEWAY,
        manifest,
        stagingDir
      })
    ).rejects.toThrow(/http:\/\/192\.168\.25\.130/)
  })

  // 白名单里的分发点要真的走完下载 + 校验，不能只是「不抛异常」。
  it('认可的分发点上的构建物照常下载并校验', async () => {
    const payload = crypto.randomBytes(64)
    const manifest = manifestFor(payload, { artifact_url: 'http://192.0.2.130/releases/app.dmg' })
    const fetchImpl = vi.fn().mockResolvedValue(bodyOf(payload))

    const saved = await downloadAndVerifyArtifact({
      allowedArtifactOrigins: ['http://192.0.2.130'],
      fetchImpl,
      gatewayOrigin: GATEWAY,
      manifest,
      stagingDir
    })

    expect(fetchImpl).toHaveBeenCalledWith('http://192.0.2.130/releases/app.dmg', expect.anything())
    expect(crypto.createHash('sha256').update(fs.readFileSync(saved)).digest('hex')).toBe(manifest.artifact_sha256)
  })

  // 分发点被控制换了内容 —— 哈希对不上就必须中止。这是放宽同源之后唯一还
  // 拦得住「托管机被控制」的那道，所以它必须由分发点这条路径独立测到。
  it('认可的分发点返回了别的字节，一样中止', async () => {
    const manifest = manifestFor(crypto.randomBytes(64), { artifact_url: 'http://192.0.2.130/releases/app.dmg' })

    await expect(
      downloadAndVerifyArtifact({
        allowedArtifactOrigins: ['http://192.0.2.130'],
        fetchImpl: vi.fn().mockResolvedValue(bodyOf(crypto.randomBytes(64))),
        gatewayOrigin: GATEWAY,
        manifest,
        stagingDir
      })
    ).rejects.toMatchObject({ code: 'hash-mismatch' })
  })

  it('下载失败时抛且不留文件', async () => {
    const payload = crypto.randomBytes(64)
    const manifest = manifestFor(payload)

    await expect(
      downloadAndVerifyArtifact({
        fetchImpl: vi.fn().mockResolvedValue({ ok: false, status: 404 } as unknown as Response),
        gatewayOrigin: GATEWAY,
        manifest,
        stagingDir
      })
    ).rejects.toMatchObject({ code: 'download-failed' })
    expect(fs.existsSync(stagingDir) ? fs.readdirSync(stagingDir) : []).toEqual([])
  })

  it('上一次失败留下的 .part 不会被当成本次结果', async () => {
    const payload = crypto.randomBytes(2048)
    const manifest = manifestFor(payload)

    const stale = path.join(
      stagingDir,
      `llano-${manifest.version}-${manifest.platform}-${manifest.arch}.dmg.part`
    )

    fs.mkdirSync(stagingDir, { recursive: true })
    fs.writeFileSync(stale, Buffer.from('上一次没下完的垃圾'))

    const staged = await downloadAndVerifyArtifact({
      fetchImpl: vi.fn().mockResolvedValue(bodyOf(payload)),
      gatewayOrigin: GATEWAY,
      manifest,
      stagingDir
    })

    expect(fs.readFileSync(staged)).toEqual(payload)
  })

  it('汇报下载进度', async () => {
    const payload = crypto.randomBytes(1024)
    const manifest = manifestFor(payload)
    const seen: Array<[number, number]> = []

    await downloadAndVerifyArtifact({
      fetchImpl: vi.fn().mockResolvedValue(bodyOf(payload)),
      gatewayOrigin: GATEWAY,
      manifest,
      onProgress: (received, total) => seen.push([received, total]),
      stagingDir
    })

    expect(seen.at(-1)).toEqual([1024, 1024])
  })

  it('UpdateVerificationError 带着可判定的 code', () => {
    const error = new UpdateVerificationError('hash-mismatch', 'x')

    expect(error).toBeInstanceOf(Error)
    expect(error.code).toBe('hash-mismatch')
  })
})
