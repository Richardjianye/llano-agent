/**
 * 交叉构建时绝不能复用本机的 electron dist（阶段 6）。
 *
 * 这条判断失手的现象特别难查：在 macOS 上打出来的 Windows 包能装、能创建
 * 快捷方式，双击没反应 —— 因为 win-unpacked 里装的是 Electron.app。
 * 构建日志里一个警告都没有，所以只能靠测试钉。
 */
import fs from 'node:fs'

import { describe, expect, it } from 'vitest'

import { artifactEnvSuffix, shouldUseLocalElectronDist } from './run-electron-builder.mjs'

describe('shouldUseLocalElectronDist', () => {
  it('不点名平台时打的就是本机，dist 可以复用', () => {
    expect(shouldUseLocalElectronDist([], 'darwin')).toBe(true)
    expect(shouldUseLocalElectronDist(['--dir'], 'darwin')).toBe(true)
  })

  it('目标就是本机平台时可以复用', () => {
    expect(shouldUseLocalElectronDist(['--mac', 'dmg'], 'darwin')).toBe(true)
    expect(shouldUseLocalElectronDist(['--win', 'nsis'], 'win32')).toBe(true)
    expect(shouldUseLocalElectronDist(['-l'], 'linux')).toBe(true)
  })

  it('交叉构建时不能复用', () => {
    expect(shouldUseLocalElectronDist(['--win', 'nsis'], 'darwin')).toBe(false)
    expect(shouldUseLocalElectronDist(['-w'], 'darwin')).toBe(false)
    expect(shouldUseLocalElectronDist(['--mac'], 'win32')).toBe(false)
    expect(shouldUseLocalElectronDist(['--linux', 'deb'], 'darwin')).toBe(false)
  })

  it('一次打多个平台时，只要有一个不是本机就不复用', () => {
    expect(shouldUseLocalElectronDist(['--mac', '--win'], 'darwin')).toBe(false)
  })

  // `nsis` / `dmg` 是目标名，不是平台名 —— 别把它们当平台标志解析。
  it('目标名不当平台标志', () => {
    expect(shouldUseLocalElectronDist(['nsis'], 'darwin')).toBe(true)
  })
})

// 上面几条测的是那个纯函数。但真正会出事的是「算出来了却没用上」——把
// `crossBuild ? null : electronDistDir()` 改回无条件取 dist，纯函数的用例
// 一条都不会红，而成品是个坏包。所以这里额外钉一下接线。
//
// 顺带钉住 isMain 守卫：没有它的话，这个文件 import 一次就真的去 spawn
// electron-builder，并在第一条用例之后 process.exit 掉整个测试进程。
describe('接线', () => {
  const source = fs.readFileSync(new URL('./run-electron-builder.mjs', import.meta.url), 'utf8')

  it('构建入口挂在 isMain 守卫后面', () => {
    expect(source).toContain('if (isMain(import.meta.url)) {')
  })

  it('算出来的结果真的接到了 electronDist 上', () => {
    const main = /if \(isMain\(import\.meta\.url\)\) \{[\s\S]*$/.exec(source)?.[0]

    expect(main, 'run-electron-builder.mjs 里找不到 isMain 入口').toBeTruthy()
    expect(main).toContain('const crossBuild = !shouldUseLocalElectronDist(forwarded, process.platform)')
    expect(main).toContain('const dist = crossBuild ? null : electronDistDir()')
  })
})

describe('成品文件名要带环境名', () => {
  /**
   * development 指向 127.0.0.1、test 指向内网测试服，两个包**行为完全不同**。
   * 文件名里没有环境的话，打完 test 会直接覆盖掉 dev，两个 dmg 一模一样。
   *
   * 失手的现象是小组内测时「有人连不上网关」，而现场没有任何线索可查 ——
   * 客户端不会告诉你它在连谁，文件名也不会。
   */
  it('未指定环境时回落到 development，而不是空串', () => {
    expect(artifactEnvSuffix('')).toBe('development')
    expect(artifactEnvSuffix(undefined)).toBe('development')
    expect(artifactEnvSuffix('  ')).toBe('development')
  })

  it('指定了就用指定的', () => {
    expect(artifactEnvSuffix('test')).toBe('test')
    expect(artifactEnvSuffix('production')).toBe('production')
  })

  it('artifactName 真的引用了这个变量', () => {
    // 包装脚本算对了没接进 package.json 的话，文件名照旧不带环境，
    // 而所有单测依然全绿 —— 本会话「守卫写对了但没接上」踩过三次。
    const pkg = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))

    expect(pkg.build.artifactName).toContain('${env.LLANO_ARTIFACT_ENV}')
  })

  it('包装脚本把变量喂给了 electron-builder', () => {
    const source = fs.readFileSync(new URL('./run-electron-builder.mjs', import.meta.url), 'utf8')

    expect(source).toContain('LLANO_ARTIFACT_ENV: productEnv')
  })
})
