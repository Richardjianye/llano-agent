#!/usr/bin/env node
/**
 * build-sidecar.mjs —— 用 PyInstaller 把 Hermes 后端冻成随包分发的 sidecar。
 *
 * 产物落在 `build/sidecar/`，由 electron-builder 的 extraResources 带进包，
 * 运行期由 `electron/product-runtime-boundaries.ts` 的 `productSidecarCommand()`
 * 找到 —— 品牌构建里唯一合法的后端来源（S6-E1 把 PATH 上的 hermes、系统 Python
 * 里的 hermes_cli 那几级全关掉了）。
 *
 * 选型与踩坑记录见 TODO.md「S6-E 打包选型实测」。这个脚本把其中三条固化下来：
 *
 * 1. **onedir，不是 onefile。** onefile 每次启动都要把整个归档解到临时目录，
 *    实测冷启动 2.2—5.6 s，onedir 是 0.50 s。而且「自解压再执行」正是启发式
 *    最爱报的形状 —— 我们要过的是火绒。
 * 2. **仓库根要显式进 `--paths`。** editable 安装（`pip install -e .`）装出来的
 *    `__editable__….finder` 路径钩子 PyInstaller 跟不动，收不到 hermes_cli。
 * 3. **构建完必须跑探针。** 静态分析静默漏掉只在函数体里 import 的依赖，
 *    而漏了之后 `serve` 照样起来。清单见 build-sidecar/required-modules.txt。
 */
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { desktopRoot } from './product-environment.mjs'
import { isMain } from './utils.mjs'
import { SIDECAR_STAMP, sourceFingerprint, binaryFingerprint } from './sidecar-stamp.mjs'

/** sidecar 可执行文件名，跟 electron/product-runtime-boundaries.ts 的常量对齐。 */
export const SIDECAR_NAME = 'llano-sidecar'

/**
 * 不随品牌客户端分发的上游内置技能（相对 `skills/` 的路径，精确到技能目录）。
 *
 * - `research/research-paper-writing`：学术论文写作模板，带 4 个 PDF。升级时 NSIS 先跑旧版
 *   卸载器删安装目录，一个无签名、从 %TEMP% 起的进程批量删 PDF，正中 360 的勒索行为
 *   特征，同事一装更新就弹「敲诈病毒」（2026-09-14）。它是整个技能目录里仅有的文档类
 *   文件，业务上也用不到。注意：已经装过的机器上那 4 个 PDF 还会在下一次升级时被删一次。
 */
export const EXCLUDED_BUNDLED_SKILLS = ['research/research-paper-writing']

/** 复制随包技能，跳过 {@link EXCLUDED_BUNDLED_SKILLS}。路径统一成 `/` 比对，Windows 构建机上同样生效。 */
export function copyBundledSkills(source, destination, excluded = EXCLUDED_BUNDLED_SKILLS, overwrite = true) {
  fs.cpSync(source, destination, {
    recursive: true,
    force: overwrite,
    errorOnExist: !overwrite,
    filter: entry => !excluded.includes(path.relative(source, entry).split(path.sep).join('/'))
  })
}

/** Only package output is localized; the source tree and user edits are untouched. */
export function localizeBundledSkills(destination, catalogPath) {
  const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'))
  const visited = new Set()
  function visit(directory) {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, item.name)
      if (item.isDirectory()) visit(filename)
      else if (item.name === 'SKILL.md') {
        const relative = path.relative(destination, directory).split(path.sep).join('/')
        const entry = catalog.skills[relative]
        if (!entry) throw new Error(`Bundled skill has no reviewed Chinese metadata: ${relative}`)
        const original = fs.readFileSync(filename, 'utf8')
        if (createHash('sha256').update(original).digest('hex') !== entry.source_sha256) {
          throw new Error(`Bundled skill changed; review its Chinese metadata: ${relative}`)
        }
        const fence = /^---\r?\n([\s\S]*?)\r?\n---(?=\r?\n|$)/.exec(original)
        if (!fence) throw new Error(`Bundled skill has no valid frontmatter: ${relative}`)
        let metadata = fence[1]
        for (const key of ['display_name', 'description']) {
          const rendered = `${key}: ${JSON.stringify(entry[key])}`
          const pattern = new RegExp(`^${key}:[^\\n]*(?:\\n[ \t]+[^\\n]*)*`, 'm')
          metadata = pattern.test(metadata) ? metadata.replace(pattern, () => rendered) : `${metadata}\n${rendered}`
        }
        const localized = `---\n${metadata}\n---${original.slice(fence[0].length)}`
        fs.writeFileSync(filename, localized)
        visited.add(relative)
      }
    }
  }
  visit(destination)
  const missing = Object.keys(catalog.skills).filter(relative => !visited.has(relative))
  if (missing.length) throw new Error(`Reviewed bundled skills missing from package: ${missing.join(', ')}`)
  return visited.size
}

/** 随包发的后端静态文案语言。跟 src/i18n/types.ts 的 Locale 是同一个决定。 */
export const SHIPPED_LOCALES = ['en', 'zh']

/** 读出清单里真正要检查的模块名（丢掉注释和空行）。 */
export function parseRequiredModules(text) {
  return text
    .split('\n')
    .map(line => line.trim())
    .filter(line => line && !line.startsWith('#'))
}

/**
 * 拼 PyInstaller 的参数。
 *
 * `hiddenImports` 里的东西是静态分析看不见、必须点名的；它们同时也在
 * required-modules.txt 里，由构建末尾的探针兜底 —— 点名可能点漏，探针不会。
 */
export function pyinstallerArgs({ distDir, entry, hiddenImports, repoRoot, workDir, discoveryManifest, copyMetadata = [] }) {
  return [
    '--noconfirm',
    '--clean',
    '--onedir',
    '--console',
    '--name',
    SIDECAR_NAME,
    '--distpath',
    distDir,
    '--workpath',
    workDir,
    '--specpath',
    workDir,
    // 见文件头第 2 条：editable 安装对静态分析不可见。
    '--paths',
    repoRoot,
    ...hiddenImports.flatMap(name => ['--hidden-import', name]),
    // 见 build-sidecar/required-metadata.txt：有些可用性检查读 dist-info 而不是 import。
    ...copyMetadata.flatMap(name => ['--copy-metadata', name]),
    ...(discoveryManifest ? ['--add-data', `${discoveryManifest}${path.delimiter}tools`] : []),
    '--add-data',
    `${path.join(repoRoot, 'tools', 'bundled_skill_chinese_catalog.json')}${path.delimiter}tools`,
    entry
  ]
}

/**
 * 出厂体积上限（MB）。超了就是构建环境脏了，不是「这版功能变多了」。
 *
 * PyInstaller 收的是「这个 Python 环境里能 import 到什么」，不是
 * 「pyproject 声明了什么」。在一台跑过 lazy install 的开发机上构建，
 * 它会把开发者碰巧装过的东西一起冻进去 —— 2026-08-28 实测：
 * 同一份代码，开发机 venv（279 包）打出 256 MB，干净 venv（141 包）打出 86 MB。
 * 多出来的是 onnxruntime 65 MB、PyAV 42 MB、botocore 22 MB 这些可选后端。
 *
 * **体积只是表象。** managed 配置里 `allow_lazy_installs=false` 的用意是
 * 「要哪个后端就在打包时进包，不许运行期拉未经审计的代码」；在脏环境上构建
 * 等于把这道闸门绕过去 —— 那 65 MB 的 onnxruntime 谁也没审过，却会随包
 * 发给全公司。
 *
 * 上限定在 140 而不是贴着 86：核心依赖本身会长，留出余量，
 * 但脏环境那个量级（256 MB）一定撞线。
 */
export const SIDECAR_MAX_MB = 140

/** 目录总字节数。用文件长度求和，与 Windows 侧 PowerShell 的口径一致。 */
export function dirSizeBytes(dir, fsImpl = fs) {
  let total = 0

  for (const entry of fsImpl.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)

    if (entry.isDirectory()) {
      total += dirSizeBytes(full, fsImpl)
    } else if (entry.isFile()) {
      total += fsImpl.statSync(full).size
    }
  }

  return total
}

/**
 * Copy the frozen sidecar into its Electron staging directory without leaking
 * build-machine paths. PyInstaller can emit absolute symlinks for duplicated
 * native libraries (notably Pillow's dylibs); codesign rejects those links
 * because their destination sits outside the final .app bundle.
 */
export function copySidecarTree(source, destination, fsImpl = fs) {
  function copyEntry(currentSource, currentDestination, ancestors) {
    const entry = fsImpl.lstatSync(currentSource)

    if (entry.isSymbolicLink()) {
      copyEntry(fsImpl.realpathSync(currentSource), currentDestination, ancestors)
      return
    }

    if (entry.isDirectory()) {
      const realSource = fsImpl.realpathSync(currentSource)
      if (ancestors.has(realSource)) {
        throw new Error(`sidecar contains a cyclic directory link: ${currentSource}`)
      }
      const nextAncestors = new Set(ancestors).add(realSource)
      fsImpl.mkdirSync(currentDestination, { recursive: true })
      for (const child of fsImpl.readdirSync(currentSource)) {
        copyEntry(
          path.join(currentSource, child),
          path.join(currentDestination, child),
          nextAncestors
        )
      }
      return
    }

    fsImpl.copyFileSync(currentSource, currentDestination)
    fsImpl.chmodSync(currentDestination, entry.mode)
  }

  copyEntry(source, destination, new Set())
}

/** 解析探针输出。缺模块时返回名字列表，齐了返回空数组。 */
export function parseProbeOutput(stdout) {
  const missing = /LLANO_PROBE_MISSING=(.*)/.exec(stdout)

  if (missing) {
    return missing[1].split(';').filter(Boolean)
  }

  if (/LLANO_PROBE_OK/.test(stdout)) {
    return []
  }

  // 既没说齐也没说缺 —— 探针自己没跑起来，不能当成通过。
  throw new Error(`sidecar 探针没有给出结果，输出如下：\n${stdout}`)
}

if (isMain(import.meta.url)) {
  const repoRoot = path.resolve(desktopRoot, '..', '..')
  const sidecarSrc = path.join(desktopRoot, 'build-sidecar')
  const distDir = path.join(desktopRoot, 'build', 'sidecar-dist')
  const outDir = path.join(desktopRoot, 'build', 'sidecar')
  const python = process.env.LLANO_SIDECAR_PYTHON || 'python3'
  const source = sourceFingerprint(repoRoot)
  // A failed rebuild must never leave the previous success marker usable.
  fs.rmSync(path.join(outDir, SIDECAR_STAMP), { force: true })

  const required = parseRequiredModules(fs.readFileSync(path.join(sidecarSrc, 'required-modules.txt'), 'utf8'))

  const requiredMetadata = parseRequiredModules(fs.readFileSync(path.join(sidecarSrc, 'required-metadata.txt'), 'utf8'))

  if (process.platform === 'win32') {
    required.push(...parseRequiredModules(fs.readFileSync(path.join(sidecarSrc, 'required-modules-win32.txt'), 'utf8')))
  }

  // Reuse source discovery's AST predicate, then ship its result alongside
  // archived Python modules. Runtime glob('*.py') cannot inspect a freeze.
  const discovered = spawnSync(python, ['-c',
    "import json; from pathlib import Path; from tools.registry import _module_registers_tools; print(json.dumps(['tools.'+p.stem for p in sorted(Path('tools').glob('*.py')) if p.name not in {'__init__.py','registry.py','mcp_tool.py'} and _module_registers_tools(p)]))"
  ], { cwd: repoRoot, encoding: 'utf8' })
  if (discovered.status !== 0) throw new Error(`Cannot generate frozen tool discovery manifest: ${discovered.stderr}`)
  const builtinModules = JSON.parse(discovered.stdout)
  const discoveryManifest = path.join(desktopRoot, 'build', 'builtin-tools.json')
  fs.mkdirSync(path.dirname(discoveryManifest), { recursive: true })
  fs.writeFileSync(discoveryManifest, `${JSON.stringify(builtinModules, null, 2)}\n`)

  console.log(`[sidecar] PyInstaller 打包中（${required.length} 个模块待自检）…`)

  const build = spawnSync(
    python,
    [
      '-m',
      'PyInstaller',
      ...pyinstallerArgs({
        distDir,
        entry: path.join(sidecarSrc, 'sidecar_entry.py'),
        // 显式收集验收清单中的运行路径，再对冻结产物逐个导入验证。
        hiddenImports: [...new Set([...required, ...builtinModules])],
        copyMetadata: requiredMetadata,
        discoveryManifest,
        repoRoot,
        workDir: path.join(desktopRoot, 'build', 'sidecar-work')
      })
    ],
    { stdio: 'inherit' }
  )

  if (build.status !== 0) {
    console.error('[sidecar] PyInstaller 失败')
    process.exit(build.status ?? 1)
  }

  const built = path.join(distDir, SIDECAR_NAME)
  const binary = path.join(built, process.platform === 'win32' ? `${SIDECAR_NAME}.exe` : SIDECAR_NAME)

  if (!fs.existsSync(binary)) {
    console.error(`[sidecar] PyInstaller 报成功但没有产物：${binary}`)
    process.exit(1)
  }

  // 出厂自检。构建日志里不会有任何线索，只能对着冻结产物逐个 import。
  const probe = spawnSync(binary, ['--llano-probe', [...required, ...requiredMetadata.map(name => `dist:${name}`)].join(',')], { encoding: 'utf8' })
  const missing = parseProbeOutput(`${probe.stdout || ''}${probe.stderr || ''}`)

  if (missing.length > 0) {
    console.error(`[sidecar] 冻结产物缺 ${missing.length} 个模块，构建作废：`)
    for (const name of missing) {
      console.error(`  - ${name}`)
    }
    console.error('[sidecar] 往 build-sidecar/required-modules.txt 对应的 --hidden-import 里补，或查为什么被漏掉。')
    process.exit(1)
  }

  fs.rmSync(outDir, { force: true, recursive: true })
  fs.mkdirSync(path.dirname(outDir), { recursive: true })
  copySidecarTree(built, outDir)

  // 随包的 skills 放在**二进制旁边**，不进 PyInstaller 的 `_internal`。
  //
  // 上游用 `Path(__file__).parent.parent / "skills"` 去找它（tools/skills_sync.py），
  // 冻结之后那条路径指向 `_internal/tools/../skills`，是错的。好在上游给打包方
  // 留了正式的口子：`HERMES_BUNDLED_SKILLS`（注释里写明是 Homebrew / Nix 在用）。
  // 由 electron/product-runtime-boundaries.ts 的 productSidecarBundledSkills()
  // 把这个目录指给后端。
  //
  // 自己复制而不是 `--add-data`，是为了让目录布局归我们管 —— `_internal` 是
  // PyInstaller 的实现细节，不该泄漏到 Electron 那边。
  //
  // 排除清单改在这里而不是删上游 `skills/` 目录：删上游文件每次同步都要重新解冲突
  // （定制纪律：冲突量 ∝ 改上游的行数），在打包这一步过滤则一行都不碰上游。
  copyBundledSkills(path.join(repoRoot, 'skills'), path.join(outDir, 'skills'))
  // 品牌自有技能追加到原随包目录；撞名时构建失败，不覆盖原技能。
  // 只写构建目录，用户目录的增量更新和编辑保护仍由 skills_sync 管理。
  copyBundledSkills(path.join(desktopRoot, 'resources', 'default-skills'), path.join(outDir, 'skills'), [], false)
  localizeBundledSkills(path.join(outDir, 'skills'), path.join(repoRoot, 'tools', 'bundled_skill_chinese_catalog.json'))

  // 静态文案目录同理（`agent/i18n.py` 的 `HERMES_BUNDLED_LOCALES`），但**只带 zh + en**，
  // 跟阶段 6 已定的「产品只发 zh + en」对齐。上游那 17 个语言里其余 15 个不发 ——
  // 不是为了省那 500 KB，是别让一个我们声明不支持的语言半翻译地存在。
  //
  // 少带文件是安全的：`SUPPORTED_LANGUAGES` 在 agent/i18n.py 里是硬编码的、不扫目录，
  // 所以用户真把 display.language 设成 ja，解析器认这个值、catalog 文件不在，
  // 于是静默回落英文（缺 key 回落 en，en 也缺就返回 key 本身，不会崩）。
  const localesOut = path.join(outDir, 'locales')

  fs.mkdirSync(localesOut, { recursive: true })
  for (const lang of SHIPPED_LOCALES) {
    fs.copyFileSync(path.join(repoRoot, 'locales', `${lang}.yaml`), path.join(localesOut, `${lang}.yaml`))
  }

  const sizeMb = dirSizeBytes(outDir) / 1024 / 1024

  if (sizeMb > SIDECAR_MAX_MB) {
    console.error(
      `[sidecar] 产物 ${sizeMb.toFixed(1)} MB，超过上限 ${SIDECAR_MAX_MB} MB。\n` +
        '  几乎可以肯定是构建环境不干净：PyInstaller 收的是这个 Python 环境里能\n' +
        '  import 到什么，跑过 lazy install 的开发机会把可选后端一起冻进去。\n' +
        '  用只装了 pyproject 依赖的 venv 重来：\n' +
        '    uv venv /tmp/build-venv && uv pip install --python /tmp/build-venv/bin/python -e . pyinstaller\n' +
        '    LLANO_SIDECAR_PYTHON=/tmp/build-venv/bin/python npm run build:sidecar\n' +
        `  确实是核心依赖长大了的话，改 SIDECAR_MAX_MB —— 但先弄清楚长的是什么。`
    )
    process.exit(1)
  }

  if (sourceFingerprint(repoRoot) !== source) {
    throw new Error('[sidecar] 构建期间源码发生变化，请重新构建。')
  }
  fs.writeFileSync(path.join(outDir, SIDECAR_STAMP), JSON.stringify({
    schemaVersion: 1, source, platform: process.platform, arch: process.arch,
    binary: binaryFingerprint(path.join(outDir, path.basename(binary)))
  }, null, 2) + '\n')

  console.log(
    `[sidecar] 完成：${outDir}（${required.length} 个模块自检通过，${sizeMb.toFixed(1)} MB）`
  )
}
