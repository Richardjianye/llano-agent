import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import beforePack, { cleanStaleAppOutDir, preserveRollbackBackup } from '../scripts/before-pack.mjs'
import { productExecutableName } from '../scripts/utils.mjs'

test('cleanStaleAppOutDir removes a populated unpacked directory', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'linux-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    // Reproduce the corrupted partial state: license + payload present,
    // electron binary missing — exactly what trips the ENOENT rename.
    fs.writeFileSync(path.join(appOutDir, 'LICENSE.electron.txt'), 'x', 'utf8')
    fs.writeFileSync(path.join(appOutDir, 'resources.pak'), 'x', 'utf8')
    fs.mkdirSync(path.join(appOutDir, 'resources'), { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'resources', 'app.asar'), 'x', 'utf8')

    const removed = cleanStaleAppOutDir(appOutDir)

    assert.equal(removed, true)
    assert.equal(fs.existsSync(appOutDir), false)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('cleanStaleAppOutDir is a no-op when the directory is absent', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-'))
  try {
    const missing = path.join(tempRoot, 'does-not-exist')
    assert.equal(cleanStaleAppOutDir(missing), false)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('cleanStaleAppOutDir ignores empty or invalid input', () => {
  assert.equal(cleanStaleAppOutDir(''), false)
  assert.equal(cleanStaleAppOutDir(undefined), false)
  assert.equal(cleanStaleAppOutDir(null), false)
  assert.equal(cleanStaleAppOutDir(42), false)
})

test(
  'beforePack default export resolves even when cleanup throws',
  // 「清理失败不能让构建挂掉」这条契约仍然成立，但 beforePack 现在**有一条**
  // 故意的 reject 路径：品牌构建缺 sidecar（assertSidecarPresent）。那是前置条件，
  // 不是清理失败 —— 缺后端的包装得上、跑不起来，构建期挂掉远比那便宜。
  // 这里声明「本次不要求 sidecar」，好让用例回到它本来要测的那件事上。
  withoutSidecarRequirement(async () => {
    // A directory path that rmSync can't remove is simulated by passing a
    // context whose appOutDir is a file the hook will try (and be allowed) to
    // remove; the contract under test is that the hook never rejects.
    await assert.doesNotReject(beforePack({ appOutDir: '', electronPlatformName: 'linux' }))
  })
)

// ─── Windows rollback preservation (#69179) ────────────────────────────────

test('preserveRollbackBackup moves a working build to .bak', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'win-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'AnyProduct.exe'), 'MZ-old-build', 'utf8')
    fs.writeFileSync(path.join(appOutDir, 'resources.pak'), 'x', 'utf8')

    const preserved = preserveRollbackBackup(appOutDir, 'AnyProduct.exe')

    assert.equal(preserved, true)
    // Original slot vacated so electron-builder stages into a clean tree...
    assert.equal(fs.existsSync(appOutDir), false)
    // ...and the previous working build is intact under .bak for rollback.
    assert.equal(
      fs.readFileSync(path.join(`${appOutDir}.bak`, 'AnyProduct.exe'), 'utf8'),
      'MZ-old-build'
    )
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('preserveRollbackBackup replaces a stale .bak from an older update', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'win-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'AnyProduct.exe'), 'current', 'utf8')
    fs.mkdirSync(`${appOutDir}.bak`, { recursive: true })
    fs.writeFileSync(path.join(`${appOutDir}.bak`, 'AnyProduct.exe'), 'two-updates-ago', 'utf8')

    assert.equal(preserveRollbackBackup(appOutDir, 'AnyProduct.exe'), true)
    assert.equal(fs.readFileSync(path.join(`${appOutDir}.bak`, 'AnyProduct.exe'), 'utf8'), 'current')
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('preserveRollbackBackup refuses a partial tree missing the product exe', () => {
  // The corrupted partial state (interrupted prior pack) must NOT become
  // rollback material — it is exactly what cleanStaleAppOutDir exists to wipe.
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'win-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'LICENSE.electron.txt'), 'x', 'utf8')

    assert.equal(preserveRollbackBackup(appOutDir, 'AnyProduct.exe'), false)
    // Tree untouched; the caller's wipe path handles it.
    assert.equal(fs.existsSync(appOutDir), true)
    assert.equal(fs.existsSync(`${appOutDir}.bak`), false)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('preserveRollbackBackup ignores missing or invalid input', () => {
  assert.equal(preserveRollbackBackup(''), false)
  assert.equal(preserveRollbackBackup(undefined), false)
  assert.equal(preserveRollbackBackup(null), false)
  assert.equal(preserveRollbackBackup(path.join(os.tmpdir(), 'does-not-exist-xyz')), false)
})

// 这两条走的是完整的 beforePack，会经过 assertSidecarPresent。它们测的是回滚，
// 不是 sidecar 闸门，所以显式声明「本次不要求 sidecar」—— 否则用例的成败取决于
// 本机 build/sidecar 里有没有东西（实测就是这么挂的：linux 那条因为
// llano-sidecar 在而过，win32 那条因为 llano-sidecar.exe 不在而挂）。
// sidecar 闸门本身由 scripts/build-sidecar.test.mjs 单独测。
function withoutSidecarRequirement(run) {
  return async () => {
    const previous = process.env.LLANO_ALLOW_SIDECARLESS_BUILD

    process.env.LLANO_ALLOW_SIDECARLESS_BUILD = '1'
    try {
      await run()
    } finally {
      if (previous === undefined) {
        delete process.env.LLANO_ALLOW_SIDECARLESS_BUILD
      } else {
        process.env.LLANO_ALLOW_SIDECARLESS_BUILD = previous
      }
    }
  }
}

test(
  'beforePack on win32 preserves the previous build instead of wiping it',
  withoutSidecarRequirement(async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'win-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    // context 里没有 packager 信息时走兜底。兜底名字必须跟 package.json 的
    // build.executableName 一致 —— 之前它写死成上游的 'Hermes'，于是改了产品名
    // 之后回滚备份会认不出上一版目录，而这里不报错、只是静默地没备份。
    const exe = `${productExecutableName()}.exe`
    fs.writeFileSync(path.join(appOutDir, exe), 'MZ-working', 'utf8')

    // node-pty staging is skipped because arch is not a number here.
    await beforePack({ appOutDir, electronPlatformName: 'win32' })

    assert.equal(fs.existsSync(appOutDir), false)
    assert.equal(fs.readFileSync(path.join(`${appOutDir}.bak`, exe), 'utf8'), 'MZ-working')
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
  })
)

test(
  'beforePack on linux keeps the plain wipe (no .bak)',
  withoutSidecarRequirement(async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'linux-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'AnyProduct.exe'), 'x', 'utf8')

    await beforePack({ appOutDir, electronPlatformName: 'linux' })

    assert.equal(fs.existsSync(appOutDir), false)
    assert.equal(fs.existsSync(`${appOutDir}.bak`), false)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
  })
)

// URL.pathname 在 Windows 上会给出 `/C:/Users/...`（盘符前多一个斜杠），
// path.join 之后变成 `\C:\Users\...`，永远不存在。
//
// 这条不是假想：before-pack.mjs 里的 sidecar 路径就是这么拼的，在 macOS 上
// 一路正常，到 Windows 构建机上表现为「sidecar 明明打出来了、体积也对，
// electron-builder 却说缺 sidecar」。而报错信息里那个路径乍看完全正常 ——
// 多出来的那一个字符要盯着看才发现。
//
// 扫整个 scripts/ 而不是只钉 before-pack.mjs：这些脚本大多只在 macOS 上跑过，
// 同一个写法在任何一个文件里都会以同样的方式坏掉，而且只在构建机上才暴露。
test('scripts/ 里不许用 new URL(...).pathname 求路径（Windows 上是坏的）', () => {
  const scriptsDir = path.join(import.meta.dirname, '..', 'scripts')
  const offenders = []

  for (const name of fs.readdirSync(scriptsDir)) {
    // 测试文件自己会在标题和说明里写出这个模式，跳过。
    if ((!name.endsWith('.mjs') && !name.endsWith('.js')) || name.includes('.test.')) {
      continue
    }

    const source = fs.readFileSync(path.join(scriptsDir, name), 'utf8')

    for (const [index, line] of source.split('\n').entries()) {
      // 注释里提到它是允许的 —— 这条测试自己的说明、以及修复处的警示注释。
      if (/new URL\([^)]*\)\.pathname/.test(line) && !/^\s*(\/\/|\*)/.test(line)) {
        offenders.push(`${name}:${index + 1}: ${line.trim()}`)
      }
    }
  }

  assert.deepEqual(
    offenders,
    [],
    `改用 fileURLToPath(import.meta.url)，或用 product-environment.mjs 导出的 desktopRoot：\n${offenders.join('\n')}`
  )
})
