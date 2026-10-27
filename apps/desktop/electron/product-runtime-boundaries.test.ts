import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { test } from 'vitest'

import {
  allowsDeveloperBackendOverride,
  allowsUserInstalledBackend,
  brandedHermesHome,
  productDeepLinkProtocol,
  productSidecarBundledLocales,
  productSidecarBundledSkills,
  productSidecarCommand
} from './product-runtime-boundaries'

test('branded Hermes state is isolated by product userData and environment', () => {
  assert.equal(
    brandedHermesHome({
      brandId: 'llano-ai-workbench',
      environment: 'development',
      productUserData: '/Users/developer/Library/Application Support/LLANO Agent'
    }),
    '/Users/developer/Library/Application Support/LLANO Agent/hermes-home/development'
  )
  assert.equal(
    brandedHermesHome({
      brandId: 'llano-ai-workbench',
      environment: 'production',
      productUserData: '/product-data'
    }),
    '/product-data/hermes-home/production'
  )
})

test('explicit homes and desktop test sandboxes keep precedence', () => {
  assert.equal(
    brandedHermesHome({
      brandId: 'llano-ai-workbench',
      environment: 'development',
      explicitHermesHome: '/developer/hermes',
      productUserData: '/product-data',
      userDataOverride: '/test-sandbox'
    }),
    '/developer/hermes'
  )
  assert.equal(
    brandedHermesHome({
      brandId: 'llano-ai-workbench',
      environment: 'test',
      productUserData: '/product-data',
      userDataOverride: '/test-sandbox'
    }),
    '/test-sandbox/hermes-home'
  )
})

test('stock Hermes keeps its legacy resolver and protocol', () => {
  assert.equal(
    brandedHermesHome({ brandId: '', environment: 'development', productUserData: '/product-data' }),
    null
  )
  assert.equal(productDeepLinkProtocol('', 'llano-agent'), 'hermes')
})

test('branded desktop uses its configured protocol', () => {
  assert.equal(productDeepLinkProtocol('llano-ai-workbench', 'llano-agent'), 'llano-agent')
})

test('packaged protocol registration matches the product policy', () => {
  const desktopRoot = path.resolve(import.meta.dirname, '..')
  const product = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'product-environments.json'), 'utf8'))
  const manifest = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))
  const registered = manifest.build.protocols.flatMap((entry: { schemes?: string[] }) => entry.schemes ?? [])

  assert.deepEqual(registered, [product.deepLinkProtocol])
})

test('branded builds refuse a backend the user installed themselves', () => {
  // 一台同时装了原版 Hermes 的机器上，第 4/5 级会抢在 fail-closed 守卫前面。
  assert.equal(allowsUserInstalledBackend('llano-ai-workbench'), false)
  // 上游构建（无品牌）保持原样：PATH 上的 hermes 与 pip 装的 hermes_cli 照用。
  assert.equal(allowsUserInstalledBackend(''), true)
  assert.equal(allowsUserInstalledBackend('   '), true)
})

test('developer backend overrides survive dev but not a shipped branded package', () => {
  assert.equal(allowsDeveloperBackendOverride('llano-ai-workbench', true), false)
  assert.equal(allowsDeveloperBackendOverride('llano-ai-workbench', false), true)
  assert.equal(allowsDeveloperBackendOverride('', true), true)
  assert.equal(allowsDeveloperBackendOverride('', false), true)
})

test('every user-writable rung of resolveHermesBackend is actually gated', () => {
  // 纯函数写对了不代表接上了 —— 「登录后无限 CONNECTING」就是守卫写对了却没生效。
  // main.ts 太大、装不进单测，所以这里直接读源码；先把注释剥掉，免得断言命中的是
  // 自己写的说明而不是代码（这个坑本轮踩过一次）。
  const source = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8')
  const start = source.indexOf('function resolveHermesBackend(')
  const end = source.indexOf('\nasync function ensureRuntime(', start)

  assert.ok(start > 0 && end > start, 'resolveHermesBackend not found in main.ts')

  const body = source
    .slice(start, end)
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n')

  // 第 1 级：开发期 checkout 覆盖
  assert.match(body, /allowDeveloperOverride &&\s*\n?\s*process\.env\.HERMES_DESKTOP_HERMES_ROOT/)
  // 第 3 级：ACTIVE_HERMES_ROOT（品牌构建从不 bootstrap，非空即用户自己塞的）
  assert.match(body, /allowUserInstalled && activeRuntime\.shouldUseActiveRuntime/)
  // 第 4 级：PATH 上的 hermes
  assert.match(body, /allowUserInstalled && process\.env\.HERMES_DESKTOP_IGNORE_EXISTING/)
  // 第 5 级：系统 Python 里的 hermes_cli
  assert.match(body, /allowUserInstalled \? findSystemPython\(\) : null/)
})

const sidecarArgs = {
  appRoot: '/app',
  brandId: 'llano-ai-workbench',
  exists: () => true,
  isPackaged: true,
  platform: 'darwin' as NodeJS.Platform,
  resourcesPath: '/app/Contents/Resources'
}

test('packaged branded builds look for the sidecar in extraResources', () => {
  assert.equal(productSidecarCommand(sidecarArgs), '/app/Contents/Resources/sidecar/llano-sidecar')
  assert.equal(
    productSidecarCommand({ ...sidecarArgs, platform: 'win32' }),
    '/app/Contents/Resources/sidecar/llano-sidecar.exe'
  )
})

test('dev runs pick the sidecar up from the build directory', () => {
  // 冻结之后才会炸的那一类问题要在 dev 就能复现，否则只有打包才暴露。
  assert.equal(productSidecarCommand({ ...sidecarArgs, isPackaged: false }), '/app/build/sidecar/llano-sidecar')
})

test('a missing sidecar resolves to null rather than something else', () => {
  // 落到 null 就是落到 fail-closed；**不能**回落到用户装的运行时。
  assert.equal(productSidecarCommand({ ...sidecarArgs, exists: () => false }), null)
})

test('upstream builds have no sidecar at all', () => {
  assert.equal(productSidecarCommand({ ...sidecarArgs, brandId: '' }), null)
  assert.equal(productSidecarCommand({ ...sidecarArgs, brandId: '  ' }), null)
})

test('the sidecar rung sits ahead of every other rung in main.ts', () => {
  // 顺序本身就是守卫的一部分 ——「无限 CONNECTING」是守卫写对了却排在上游的门后面。
  const source = fs
    .readFileSync(path.join(__dirname, 'main.ts'), 'utf8')
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n')

  const start = source.indexOf('function resolveHermesBackend(')
  const end = source.indexOf('\nasync function ensureRuntime(', start)
  const body = source.slice(start, end)

  // 锚在 **return** 上，不是在查找上。变异验证抓到过：把整个 `if (sidecar)`
  // 分支挪到 PATH 探测后面，而 `const sidecar = …` 留在原地 —— 顺序实质上坏了，
  // 断言却照样绿，因为它量的是「什么时候去找」而不是「什么时候返回」。
  assert.ok(body.includes('productSidecarCommand('), 'resolveHermesBackend 没接上 productSidecarCommand')

  const sidecarAt = body.indexOf('if (sidecar) {')

  assert.ok(sidecarAt > 0, 'sidecar 那一级没有提前 return')

  for (const later of ['HERMES_DESKTOP_HERMES_ROOT', 'activeRuntimeState()', "findOnPath('hermes')", 'findSystemPython()']) {
    assert.ok(body.indexOf(later) > sidecarAt, `sidecar 必须排在 ${later} 前面`)
  }
})

test('bundled skills sit next to the sidecar binary, not inside PyInstaller internals', () => {
  // `_internal` 是 PyInstaller 的实现细节，不该泄漏到 Electron 这边。
  const skills = productSidecarBundledSkills('/res/sidecar/llano-sidecar')

  assert.equal(skills, '/res/sidecar/skills')
  assert.doesNotMatch(skills, /_internal/)
})

test('the sidecar rung hands the backend its bundled skills', () => {
  // 冻结之后上游那句 Path(__file__).parent.parent / "skills" 指向错地方，
  // 不传这个环境变量的话技能库就是空的 —— 而且不会报错。
  const source = fs
    .readFileSync(path.join(__dirname, 'main.ts'), 'utf8')
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n')

  const start = source.indexOf('function resolveHermesBackend(')
  const end = source.indexOf('\nasync function ensureRuntime(', start)

  assert.match(source.slice(start, end), /HERMES_BUNDLED_SKILLS: productSidecarBundledSkills\(sidecar\)/)
  assert.match(source.slice(start, end), /HERMES_BUNDLED_LOCALES: productSidecarBundledLocales\(sidecar\)/)
})

test('bundled locales sit next to the sidecar binary too', () => {
  assert.equal(productSidecarBundledLocales('/res/sidecar/llano-sidecar'), '/res/sidecar/locales')
})
