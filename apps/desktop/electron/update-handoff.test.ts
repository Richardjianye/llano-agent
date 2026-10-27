import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import { HandoffError, handOffToInstaller, planInstallerHandoff, supportsSilentInstall } from './update-handoff'
import { normalizeManifest } from './update-manifest'

const vectorsPath = path.resolve(__dirname, '../../../audit-gateway/release/manifest-vectors.json')
const vectors = JSON.parse(fs.readFileSync(vectorsPath, 'utf8'))
const MANIFEST = normalizeManifest(vectors.cases[0].manifest)

function harness(spawnBehaviour?: () => never) {
  const events: string[] = []
  const unref = vi.fn(() => events.push('unref'))

  const spawnImpl = vi.fn(
    (command: string, args: string[], _options: { detached: boolean; stdio: 'ignore' }) => {
      events.push(`spawn:${command} ${args.join(' ')}`.trim())
      spawnBehaviour?.()

      return { unref }
    }
  )

  const quitImpl = vi.fn(() => events.push('quit'))

  return { events, quitImpl, spawnImpl }
}

describe('交接计划', () => {
  it('macOS 用 open 打开磁盘映像', () => {
    expect(planInstallerHandoff('/tmp/app.dmg', 'darwin')).toEqual({
      args: ['/tmp/app.dmg'],
      command: 'open',
      silent: false
    })
  })

  it('Windows 默认不加静默参数（用户看得见进度，装失败看得见错误）', () => {
    const plan = planInstallerHandoff('C:\\tmp\\app.exe', 'win32')

    expect(plan.command).toBe('C:\\tmp\\app.exe')
    expect(plan.args).toEqual([])
    expect(plan.silent).toBe(false)
  })

  it('Windows 静默安装要同时带 /S 和 --force-run', () => {
    const plan = planInstallerHandoff('C:\\tmp\\app.exe', 'win32', { silent: true })

    // 少了 --force-run 的表现是"应用自己关了再也没回来"：assisted 安装器
    // 只在 isForceRun && Silent 同时成立时才把应用重新拉起来
    expect(plan.args).toEqual(['/S', '--force-run'])
    expect(plan.silent).toBe(true)
  })

  it('macOS 上传 silent 也不会假装静默', () => {
    // dmg 只能挂载让用户拖。这里要是把 silent 原样回传，上层就会去说
    // "装完会自动重开"，而实际上什么都没发生。
    const plan = planInstallerHandoff('/tmp/app.dmg', 'darwin', { silent: true })

    expect(plan.silent).toBe(false)
    expect(plan.command).toBe('open')
  })

  it('只有 Windows 支持静默安装', () => {
    expect(supportsSilentInstall('win32')).toBe(true)
    expect(supportsSilentInstall('darwin')).toBe(false)
    expect(supportsSilentInstall('linux')).toBe(false)
  })

  it('不认识的平台直接抛', () => {
    expect(() => planInstallerHandoff('/tmp/app.AppImage', 'linux')).toThrow(HandoffError)
  })
})

describe('交接执行', () => {
  it('先起安装器，成功了才退出自己', () => {
    // 顺序反过来 = "应用关了、更新也没装上"，用户手里剩一个打不开的东西
    const { events, quitImpl, spawnImpl } = harness()

    const result = handOffToInstaller({
      artifactPath: '/tmp/app.dmg',
      manifest: MANIFEST,
      platform: 'darwin',
      quitImpl,
      spawnImpl
    })

    expect(result).toMatchObject({ handedOff: true, ok: true, version: '1.4.0' })
    expect(events).toEqual(['spawn:open /tmp/app.dmg', 'unref', 'quit'])
  })

  it('安装器起不来就不退出，旧版本继续能用', () => {
    const { events, quitImpl, spawnImpl } = harness(() => {
      throw new Error('ENOENT')
    })

    const result = handOffToInstaller({
      artifactPath: '/tmp/app.dmg',
      manifest: MANIFEST,
      platform: 'darwin',
      quitImpl,
      spawnImpl
    })

    expect(result).toMatchObject({ code: 'spawn-failed', handedOff: false, ok: false })
    expect(result.message).toContain('保留当前版本')
    expect(quitImpl).not.toHaveBeenCalled()
    expect(events).toEqual(['spawn:open /tmp/app.dmg'])
  })

  it('平台不支持时既不起进程也不退出', () => {
    const { quitImpl, spawnImpl } = harness()

    const result = handOffToInstaller({
      artifactPath: '/tmp/app.AppImage',
      manifest: MANIFEST,
      platform: 'linux',
      quitImpl,
      spawnImpl
    })

    expect(result).toMatchObject({ code: 'unsupported-platform', handedOff: false, ok: false })
    expect(spawnImpl).not.toHaveBeenCalled()
    expect(quitImpl).not.toHaveBeenCalled()
  })

  it('安装器必须 detached 且脱离 stdio，否则跟着我们一起被杀掉', () => {
    const { quitImpl, spawnImpl } = harness()

    handOffToInstaller({
      artifactPath: '/tmp/app.dmg',
      manifest: MANIFEST,
      platform: 'darwin',
      quitImpl,
      spawnImpl
    })

    expect(spawnImpl.mock.calls[0][2]).toEqual({ detached: true, stdio: 'ignore' })
  })
})


describe('静默交接', () => {
  it('静默安装照样是先起安装器再退出自己', () => {
    const { events, quitImpl, spawnImpl } = harness()

    const result = handOffToInstaller({
      artifactPath: 'C:\\tmp\\app.exe',
      manifest: { ...MANIFEST, platform: 'win32' },
      platform: 'win32',
      quitImpl,
      silent: true,
      spawnImpl
    })

    expect(result).toMatchObject({ handedOff: true, ok: true, silent: true })
    expect(events).toEqual(['spawn:C:\\tmp\\app.exe /S --force-run', 'unref', 'quit'])
    expect(result.message).toContain('自动重新打开')
  })

  it('静默安装器起不来时同样不退出', () => {
    const { events, quitImpl, spawnImpl } = harness(() => {
      throw new Error('EPERM')
    })

    const result = handOffToInstaller({
      artifactPath: 'C:\\tmp\\app.exe',
      manifest: { ...MANIFEST, platform: 'win32' },
      platform: 'win32',
      quitImpl,
      silent: true,
      spawnImpl
    })

    expect(result).toMatchObject({ handedOff: false, code: 'spawn-failed', ok: false })
    expect(events).not.toContain('quit')
  })
})
