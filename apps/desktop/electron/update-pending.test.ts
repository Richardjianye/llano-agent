import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  clearPendingUpdate,
  inspectPendingUpdate,
  pendingRecordPath,
  pendingUpdateFrom,
  readPendingUpdate,
  sha256File,
  writePendingUpdate
} from './update-pending'

let dir = ''

const bytes = Buffer.from('installer-bytes')
const digest = crypto.createHash('sha256').update(bytes).digest('hex')

const isNewer = (candidate: string, current: string) => {
  const parse = (v: string) => v.split('.').map(Number)
  const [a, b] = [parse(candidate), parse(current)]

  for (let i = 0; i < 3; i += 1) {
    if ((a[i] || 0) !== (b[i] || 0)) {
      return (a[i] || 0) > (b[i] || 0)
    }
  }

  return false
}

function stageArtifact(name = 'llano-0.17.14-win32-x64.exe') {
  const file = path.join(dir, name)

  fs.writeFileSync(file, bytes)

  return file
}

function record(overrides: Record<string, unknown> = {}) {
  return {
    arch: 'x64',
    artifactPath: path.join(dir, 'llano-0.17.14-win32-x64.exe'),
    platform: 'win32',
    policy: 'required' as const,
    schema: 1,
    sha256: digest,
    size: bytes.length,
    stagedAt: 1_700_000_000_000,
    version: '0.17.14',
    ...overrides
  }
}

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pending-update-'))
})

afterEach(() => {
  fs.rmSync(dir, { force: true, recursive: true })
})

describe('待装记录的读写', () => {
  it('写进去能原样读回来', () => {
    writePendingUpdate(dir, record())

    expect(readPendingUpdate(dir)).toMatchObject({ policy: 'required', sha256: digest, version: '0.17.14' })
  })

  it('从清单生成记录时，哈希与体积抄的是清单里的值', () => {
    const manifest = {
      arch: 'x64',
      artifact_sha256: digest,
      artifact_size: bytes.length,
      platform: 'win32',
      policy: 'required',
      version: '0.17.14'
    }

    const made = pendingUpdateFrom(manifest as never, '/tmp/x.exe', () => 42)

    expect(made).toMatchObject({ sha256: digest, size: bytes.length, stagedAt: 42, version: '0.17.14' })
  })

  it('记录坏掉一律当作没有待装，而不是抛异常', () => {
    fs.writeFileSync(pendingRecordPath(dir), '{ this is not json')

    expect(readPendingUpdate(dir)).toBeNull()
  })

  it('schema 变了的旧记录不再解释', () => {
    writePendingUpdate(dir, record({ schema: 999 }) as never)

    expect(readPendingUpdate(dir)).toBeNull()
  })

  it('缺字段、体积非法的记录都读成 null', () => {
    writePendingUpdate(dir, record({ sha256: '' }) as never)
    expect(readPendingUpdate(dir)).toBeNull()

    writePendingUpdate(dir, record({ size: 0 }) as never)
    expect(readPendingUpdate(dir)).toBeNull()

    writePendingUpdate(dir, record({ policy: 'whatever' }) as never)
    expect(readPendingUpdate(dir)).toBeNull()
  })
})

describe('启动时判断待装记录还能不能用', () => {
  it('文件在、哈希对、版本更新 → 可用', () => {
    stageArtifact()
    writePendingUpdate(dir, record())

    const checked = inspectPendingUpdate(dir, {
      arch: 'x64',
      currentVersion: '0.17.13',
      isNewer,
      platform: 'win32'
    })

    expect(checked).toMatchObject({ usable: true })
    expect(checked.record?.version).toBe('0.17.14')
  })

  it('暂存文件被换掉（哈希对不上）→ 拒绝', () => {
    const file = stageArtifact()

    writePendingUpdate(dir, record())
    // 下载时验过签、验过哈希，但那是上次开机之前的事。这中间文件被改了。
    fs.writeFileSync(file, Buffer.from('evil-payload-of-the-same-length'.slice(0, bytes.length)))

    expect(inspectPendingUpdate(dir, { arch: 'x64', currentVersion: '0.17.13', isNewer, platform: 'win32' })).toMatchObject({
      reason: 'hash-mismatch',
      usable: false
    })
  })

  it('体积对不上时连哈希都不算，直接拒绝', () => {
    const file = stageArtifact()

    writePendingUpdate(dir, record())
    fs.writeFileSync(file, Buffer.concat([bytes, Buffer.from('extra')]))

    let hashed = 0

    const checked = inspectPendingUpdate(dir, {
      arch: 'x64',
      currentVersion: '0.17.13',
      hashImpl: file2 => {
        hashed += 1

        return sha256File(file2)
      },
      isNewer,
      platform: 'win32'
    })

    expect(checked).toMatchObject({ reason: 'size-mismatch', usable: false })
    expect(hashed).toBe(0)
  })

  it('暂存文件没了 → 拒绝', () => {
    writePendingUpdate(dir, record())

    expect(inspectPendingUpdate(dir, { arch: 'x64', currentVersion: '0.17.13', isNewer, platform: 'win32' })).toMatchObject({
      reason: 'missing-artifact',
      usable: false
    })
  })

  it('已经装上这一版了 → 不再重复装（装完记录还在，安装器不认识它）', () => {
    stageArtifact()
    writePendingUpdate(dir, record())

    expect(inspectPendingUpdate(dir, { arch: 'x64', currentVersion: '0.17.14', isNewer, platform: 'win32' })).toMatchObject({
      reason: 'already-installed',
      usable: false
    })
  })

  it('平台或架构对不上 → 拒绝（记录被人搬过来了）', () => {
    stageArtifact()
    writePendingUpdate(dir, record())

    expect(inspectPendingUpdate(dir, { arch: 'arm64', currentVersion: '0.17.13', isNewer, platform: 'darwin' })).toMatchObject({
      reason: 'platform-mismatch',
      usable: false
    })
  })

  it('没有记录时不报错，reason 是 no-pending', () => {
    expect(inspectPendingUpdate(dir, { currentVersion: '0.17.13', isNewer })).toMatchObject({
      reason: 'no-pending',
      usable: false
    })
  })
})

describe('清理', () => {
  it('默认连暂存文件一起删', () => {
    const file = stageArtifact()

    writePendingUpdate(dir, record())
    clearPendingUpdate(dir)

    expect(fs.existsSync(file)).toBe(false)
    expect(readPendingUpdate(dir)).toBeNull()
  })

  it('removeArtifact:false 时只删记录 —— 安装器正在读那个文件', () => {
    const file = stageArtifact()

    writePendingUpdate(dir, record())
    clearPendingUpdate(dir, { removeArtifact: false })

    expect(fs.existsSync(file)).toBe(true)
    expect(readPendingUpdate(dir)).toBeNull()
  })

  it('记录不存在时清理也不抛', () => {
    expect(() => clearPendingUpdate(dir)).not.toThrow()
  })
})
