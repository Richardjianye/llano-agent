import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  canonicalManifestBytes,
  compareVersions,
  manifestAppliesTo,
  ManifestError,
  normalizeManifest,
  parseVersion,
  publicKeyFingerprint,
  SIGNING_DOMAIN,
  updateIsMandatory,
  verifyManifest
} from './update-manifest'

/**
 * 固定向量与网关、Python 客户端共用同一个文件。
 *
 * 这一组是本文件的立身之本：三份实现之间没有 import 关系，规范字节漂开的后果
 * 不是某台机器出错，是**全量客户端同时验签失败**。
 */
const vectorsPath = path.resolve(__dirname, '../../../audit-gateway/release/manifest-vectors.json')
const vectors = JSON.parse(fs.readFileSync(vectorsPath, 'utf8'))
const PUBLIC_KEY: string = vectors.test_public_key
const PRIVATE_KEY: string = vectors.test_private_key

const CASES: Array<{ canonical: string; manifest: Record<string, unknown>; name: string; signature: string }> =
  vectors.cases

function manifest(index = 0) {
  return { ...CASES[index].manifest }
}

describe('固定向量：三份实现必须逐字节一致', () => {
  it.each(CASES)('$name 的规范字节没有漂', testCase => {
    expect(canonicalManifestBytes(testCase.manifest).toString('utf8')).toBe(testCase.canonical)
  })

  it.each(CASES)('$name 的签名验得过', testCase => {
    expect(verifyManifest(testCase.manifest, testCase.signature, PUBLIC_KEY)).toBe(true)
  })

  it('签名域与固定向量一致', () => {
    expect(SIGNING_DOMAIN).toBe(vectors.signing_domain)
  })

  it('裸 JSON 的签名不能被当成清单签名', () => {
    const body = canonicalManifestBytes(manifest())
    const naked = body.subarray(SIGNING_DOMAIN.length + 1)

    const key = crypto.createPrivateKey({
      format: 'jwk',
      key: {
        crv: 'Ed25519',
        d: Buffer.from(PRIVATE_KEY, 'base64').toString('base64url'),
        kty: 'OKP',
        x: Buffer.from(PUBLIC_KEY, 'base64').toString('base64url')
      }
    })

    const signature = crypto.sign(null, naked, key).toString('base64')

    expect(verifyManifest(manifest(), signature, PUBLIC_KEY)).toBe(false)
  })
})

describe('篡改', () => {
  it.each([
    ['version', '9.9.9'],
    ['platform', 'win32'],
    ['arch', 'x64'],
    ['policy', 'optional'],
    ['min_supported_version', '1.3.0'],
    ['artifact_url', 'https://evil.example.com/x.dmg'],
    ['artifact_sha256', 'b'.repeat(64)],
    ['artifact_size', 182452225],
    ['release_notes', '被改过的发行说明'],
    ['released_at', '2026-08-25T02:30:00Z']
  ])('改了 %s 就验不过', (field, tampered) => {
    const tamperedManifest = { ...manifest(), [field]: tampered }

    expect(tamperedManifest[field as keyof typeof tamperedManifest]).not.toBe(
      manifest()[field as keyof ReturnType<typeof manifest>]
    )
    expect(verifyManifest(tamperedManifest, CASES[0].signature, PUBLIC_KEY)).toBe(false)
  })

  it('换一把公钥验不过', () => {
    const other = crypto.generateKeyPairSync('ed25519').publicKey.export({ format: 'der', type: 'spki' }).subarray(12)

    expect(verifyManifest(manifest(), CASES[0].signature, other.toString('base64'))).toBe(false)
  })

  it.each([['', null, '不是 base64', 'AAAA', Buffer.alloc(64).toString('base64')]].flat())(
    '烂签名 %s 返回 false 而不是抛',
    signature => {
      expect(verifyManifest(manifest(), signature, PUBLIC_KEY)).toBe(false)
    }
  )

  it.each([['', null, 'zzz', Buffer.alloc(31).toString('base64')]].flat())('烂公钥 %s 返回 false', key => {
    expect(verifyManifest(manifest(), CASES[0].signature, key)).toBe(false)
  })

  it('清单字段不合法时返回 false 而不是抛', () => {
    expect(verifyManifest({ ...manifest(), artifact_size: -1 }, CASES[0].signature, PUBLIC_KEY)).toBe(false)
  })
})

describe('归一化', () => {
  it('是幂等的', () => {
    const once = normalizeManifest(manifest())

    expect(normalizeManifest(once)).toEqual(once)
  })

  it('丢掉未知字段，老签名仍然有效', () => {
    const withExtra = { ...manifest(), ui_badge: '热门' }

    expect(normalizeManifest(withExtra)).not.toHaveProperty('ui_badge')
    expect(verifyManifest(withExtra, CASES[0].signature, PUBLIC_KEY)).toBe(true)
  })

  it('字段顺序不影响规范字节', () => {
    const reordered = Object.fromEntries(Object.entries(manifest()).reverse())

    expect(canonicalManifestBytes(reordered)).toEqual(canonicalManifestBytes(manifest()))
  })

  it('大小写与空白被归一化后仍然验得过', () => {
    const loose = {
      ...manifest(),
      arch: 'ARM64',
      artifact_sha256: (manifest().artifact_sha256 as string).toUpperCase(),
      platform: ' DARWIN ',
      policy: 'Required'
    }

    expect(verifyManifest(loose, CASES[0].signature, PUBLIC_KEY)).toBe(true)
  })

  it.each([
    ['platform', 'linux'],
    ['arch', 'riscv'],
    ['policy', 'silent'],
    ['artifact_url', 'ftp://gw/x.dmg'],
    ['artifact_sha256', 'z'.repeat(64)],
    ['artifact_sha256', 'a'.repeat(63)],
    ['artifact_size', 0],
    ['artifact_size', '182452224'],
    ['artifact_size', 1.5],
    ['released_at', '2026-08-24 02:30:00'],
    ['released_at', '2026-08-24T02:30:00+08:00'],
    ['version', '1.4.0-rc1'],
    ['version', 'v1.4.0'],
    ['version', '1.2.3.4.5']
  ])('非法的 %s=%s 被拒绝', (field, value) => {
    expect(() => normalizeManifest({ ...manifest(), [field]: value })).toThrow(ManifestError)
  })

  it('最低可用版本不能高于本版本', () => {
    expect(() => normalizeManifest({ ...manifest(), min_supported_version: '9.0.0' })).toThrow(ManifestError)
  })

  it('不支持的 schema 被拒', () => {
    expect(() => normalizeManifest({ ...manifest(), schema: 2 })).toThrow(ManifestError)
  })
})

describe('版本比较', () => {
  it('同一版本的两种写法相等', () => {
    expect(parseVersion('1.2')).toEqual(parseVersion('1.2.0.0'))
    expect(compareVersions('1.2', '1.2.0.0')).toBe(0)
  })

  it.each([
    ['1.2.0', '1.10.0'],
    ['1.9.9', '2.0'],
    ['1.2.3', '1.2.3.1'],
    ['0.1', '1.0']
  ])('%s < %s（按数字而不是字典序）', (low, high) => {
    expect(compareVersions(low, high)).toBe(-1)
    expect(compareVersions(high, low)).toBe(1)
  })
})

describe('适用与强制判定', () => {
  it('同版本不算有更新', () => {
    expect(manifestAppliesTo(manifest(), { arch: 'arm64', currentVersion: '1.4.0', platform: 'darwin' })).toBe(false)
  })

  it('更旧的版本才算有更新', () => {
    expect(manifestAppliesTo(manifest(), { arch: 'arm64', currentVersion: '1.3.9', platform: 'darwin' })).toBe(true)
  })

  it('更新的客户端不会被推回旧版', () => {
    expect(manifestAppliesTo(manifest(), { arch: 'arm64', currentVersion: '1.5.0', platform: 'darwin' })).toBe(false)
  })

  it.each([
    ['win32', 'arm64'],
    ['darwin', 'x64'],
    ['win32', 'x64']
  ])('平台 %s 架构 %s 对不上一律不适用', (platform, arch) => {
    expect(manifestAppliesTo(manifest(), { arch, currentVersion: '1.0.0', platform })).toBe(false)
  })

  it('策略 required 就强制', () => {
    expect(updateIsMandatory(manifest(), '1.3.0')).toBe(true)
  })

  it('策略 optional 且版本达标时不强制', () => {
    expect(updateIsMandatory({ ...manifest(), policy: 'optional' }, '1.3.0')).toBe(false)
  })

  it('策略 optional 但低于最低可用版本仍然强制', () => {
    expect(updateIsMandatory({ ...manifest(), policy: 'optional' }, '1.1.9')).toBe(true)
  })

  it('恰好等于最低可用版本不强制', () => {
    expect(updateIsMandatory({ ...manifest(), policy: 'optional' }, '1.2.0')).toBe(false)
  })
})

describe('公钥指纹', () => {
  it('与 Python 侧算出来的一致', () => {
    // 网关后台显示的就是这个值，客户端与网关对不上时它是唯一的线索
    expect(publicKeyFingerprint(PUBLIC_KEY)).toBe('56475aa75463474c')
  })

  it.each([['', null, '不是密钥', 'None', Buffer.alloc(31).toString('base64')]].flat())(
    '不是 32 字节的 %s 算不出指纹',
    bad => {
      expect(publicKeyFingerprint(bad)).toBeNull()
    }
  )
})

/**
 * 跨语言常量对照
 *
 * 固定向量只钉住**序列化那一段**。三份实现里其余的判定常量 —— 版本号语法、
 * 发行说明上限、体积上限 —— 向量一个都碰不到。U2 那轮变异验证已经证明过这一点：
 * 只改客户端一侧的 RELEASE_NOTES_MAX_CHARS，固定向量全绿。
 *
 * Python 那两份之间是逐字比对源码，这里跨语言比不了文本，退而求其次比常量。
 * 比不到的东西（比如正则的具体写法）就老老实实承认比不到，别假装覆盖了。
 */
describe('与 Python 侧的常量对照', () => {
  const pythonSource = fs.readFileSync(
    path.resolve(__dirname, '../../../audit-gateway/server/services/release_manifest.py'),
    'utf8'
  )

  it.each([
    ['SIGNING_DOMAIN', `SIGNING_DOMAIN = b"${SIGNING_DOMAIN}"`],
    ['RELEASE_NOTES_MAX_CHARS', 'RELEASE_NOTES_MAX_CHARS = 4000'],
    ['ARTIFACT_SIZE_MAX', 'ARTIFACT_SIZE_MAX = 4 * 1024 * 1024 * 1024'],
    ['SCHEMA_VERSION', 'SCHEMA_VERSION = 1'],
    ['版本号语法', 'r"^\\d+(?:\\.\\d+){0,3}$"'],
    ['sha256 语法', 'r"^[0-9a-f]{64}$"']
  ])('%s 与网关那份一致', (_label, expectedLine) => {
    expect(pythonSource).toContain(expectedLine)
  })

  it('平台与架构取值范围一致', () => {
    expect(pythonSource).toContain('PLATFORMS = frozenset({"darwin", "win32"})')
    expect(pythonSource).toContain('ARCHES = frozenset({"x64", "arm64"})')
  })
})
