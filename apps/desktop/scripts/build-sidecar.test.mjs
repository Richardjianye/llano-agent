import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { describe, expect, it, test } from 'vitest'

import { assertSidecarPresent } from './before-pack.mjs'
import {
  SHIPPED_LOCALES,
  SIDECAR_MAX_MB,
  SIDECAR_NAME,
  copyBundledSkills,
  localizeBundledSkills,
  copySidecarTree,
  dirSizeBytes,
  parseProbeOutput,
  parseRequiredModules,
  pyinstallerArgs
} from './build-sidecar.mjs'

test.skipIf(process.platform === 'win32')('copySidecarTree materializes absolute build-machine symlinks', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sidecar-copy-'))

  try {
    const external = path.join(root, 'external.dylib')
    const source = path.join(root, 'source')
    const destination = path.join(root, 'destination')
    fs.mkdirSync(source)
    fs.writeFileSync(external, 'native-library')
    fs.symlinkSync(external, path.join(source, 'libnative.dylib'))

    copySidecarTree(source, destination)

    const copied = path.join(destination, 'libnative.dylib')
    assert.equal(fs.lstatSync(copied).isSymbolicLink(), false)
    assert.equal(fs.readFileSync(copied, 'utf8'), 'native-library')
  } finally {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

describe('出厂自检清单', () => {
  it('剥掉注释和空行，只留模块名', () => {
    expect(parseRequiredModules('# 说明\n\nopenai\n  jinja2  \n# 又一条注释\ntqdm\n')).toEqual([
      'openai',
      'jinja2',
      'tqdm'
    ])
  })

  it('仓库里那份清单确实盖住了静态分析漏掉过的三个', () => {
    // 基线先立：清单本身得是有内容的，否则下面那句"都在里面"是空转的。
    const required = parseRequiredModules(
      fs.readFileSync(path.join(__dirname, '..', 'build-sidecar', 'required-modules.txt'), 'utf8')
    )

    expect(required.length).toBeGreaterThan(20)
    for (const name of ['jinja2', 'tenacity', 'tqdm', 'openai', 'hermes_cli.web_server']) {
      expect(required).toContain(name)
    }
  })
})

describe('PyInstaller 参数', () => {
  it('ships the build-derived tool manifest in the frozen tools package', () => {
    const manifestArgs = pyinstallerArgs({ distDir: '/d', entry: '/entry', hiddenImports: [], repoRoot: '/repo', workDir: '/w', discoveryManifest: '/build/builtin-tools.json' })
    expect(manifestArgs[manifestArgs.indexOf('--add-data') + 1]).toBe(`/build/builtin-tools.json${path.delimiter}tools`)
  })
  const args = pyinstallerArgs({
    distDir: '/d',
    entry: '/src/sidecar_entry.py',
    hiddenImports: ['jinja2', 'tenacity'],
    repoRoot: '/repo',
    workDir: '/w'
  })

  it('打 onedir 而不是 onefile', () => {
    // onefile 冷启动 2.2—5.6 s（每次解归档），且自解压再执行正是启发式最爱报的形状。
    expect(args).toContain('--onedir')
    expect(args).not.toContain('--onefile')
  })

  it('仓库根显式进 --paths', () => {
    // editable 安装装出来的路径钩子 PyInstaller 跟不动，不给 --paths 就收不到 hermes_cli。
    expect(args[args.indexOf('--paths') + 1]).toBe('/repo')
  })

  it('每个 hidden import 都点名', () => {
    expect(args.filter(a => a === '--hidden-import')).toHaveLength(2)
    expect(args[args.indexOf('--hidden-import') + 1]).toBe('jinja2')
  })

  it('每个需要元数据的包都带 --copy-metadata', () => {
    const withMetadata = pyinstallerArgs({
      distDir: '/d',
      entry: '/entry',
      hiddenImports: [],
      repoRoot: '/repo',
      workDir: '/w',
      copyMetadata: ['python-telegram-bot', 'tornado']
    })

    const copied = withMetadata.flatMap((arg, i) => (arg === '--copy-metadata' ? [withMetadata[i + 1]] : []))
    expect(copied).toEqual(['python-telegram-bot', 'tornado'])
    expect(args).not.toContain('--copy-metadata')
  })

  it('入口在最后一位', () => {
    expect(args.at(-1)).toBe('/src/sidecar_entry.py')
  })
})

describe('探针输出', () => {
  it('齐了返回空', () => {
    expect(parseProbeOutput('LLANO_PROBE_OK\n')).toEqual([])
  })

  it('缺了就把名字报出来', () => {
    expect(parseProbeOutput('LLANO_PROBE_MISSING=jinja2 (ModuleNotFoundError);tqdm (ImportError)')).toEqual([
      'jinja2 (ModuleNotFoundError)',
      'tqdm (ImportError)'
    ])
  })

  it('探针自己没跑起来时抛错，不当成通过', () => {
    // 这条是整个自检的命门：静默少收本来就没有任何信号，
    // 要是探针挂了还算通过，等于把唯一的信号也关掉。
    expect(() => parseProbeOutput('')).toThrow(/没有给出结果/)
    expect(() => parseProbeOutput('Traceback (most recent call last):')).toThrow(/没有给出结果/)
  })
})

describe('品牌发布构建必须带 sidecar', () => {
  function makeCompleteSidecar() {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'complete-sidecar-'))
    const sidecarDir = path.join(root, SIDECAR_NAME)
    fs.writeFileSync(sidecarDir, 'binary')
    fs.mkdirSync(path.join(root, '_internal'))
    fs.mkdirSync(path.join(root, 'skills', 'productivity', 'xlsx'), { recursive: true })
    fs.writeFileSync(path.join(root, 'skills', 'productivity', 'xlsx', 'SKILL.md'), '# xlsx')
    fs.mkdirSync(path.join(root, 'locales'))
    fs.writeFileSync(path.join(root, 'locales', 'zh.yaml'), 'language: zh')
    return { root, sidecarDir }
  }

  it('二进制、非空技能树和语言目录齐全才放行', () => {
    const fixture = makeCompleteSidecar()
    try {
      assert.doesNotThrow(() =>
        assertSidecarPresent({
          allowMissing: false,
          brandId: 'llano-ai-workbench',
          sidecarDir: fixture.sidecarDir
        })
      )
    } finally {
      fs.rmSync(fixture.root, { force: true, recursive: true })
    }
  })

  it('没有就让构建失败', () => {
    assert.throws(
      () =>
        assertSidecarPresent({
          allowMissing: false,
          brandId: 'llano-ai-workbench',
          sidecarDir: path.join(os.tmpdir(), 'missing-sidecar', SIDECAR_NAME)
        }),
      /缺少 sidecar/
    )
  })

  it('只有二进制但缺少 skills 时让构建失败', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'binary-only-sidecar-'))
    const sidecarDir = path.join(root, SIDECAR_NAME)
    try {
      fs.writeFileSync(sidecarDir, 'binary')
      fs.mkdirSync(path.join(root, 'locales'))
      assert.throws(
        () => assertSidecarPresent({ allowMissing: false, brandId: 'llano-ai-workbench', sidecarDir }),
        /skills/
      )
    } finally {
      fs.rmSync(root, { force: true, recursive: true })
    }
  })

  it('skills 目录没有 SKILL.md 时让构建失败', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'empty-skills-sidecar-'))
    const sidecarDir = path.join(root, SIDECAR_NAME)
    try {
      fs.writeFileSync(sidecarDir, 'binary')
      fs.mkdirSync(path.join(root, 'skills'))
      fs.mkdirSync(path.join(root, 'locales'))
      assert.throws(
        () => assertSidecarPresent({ allowMissing: false, brandId: 'llano-ai-workbench', sidecarDir }),
        /SKILL\.md/
      )
    } finally {
      fs.rmSync(root, { force: true, recursive: true })
    }
  })

  it('缺少 locales 时让构建失败', () => {
    const fixture = makeCompleteSidecar()
    try {
      fs.rmSync(path.join(fixture.root, 'locales'), { force: true, recursive: true })
      assert.throws(
        () =>
          assertSidecarPresent({
            allowMissing: false,
            brandId: 'llano-ai-workbench',
            sidecarDir: fixture.sidecarDir
          }),
        /locales/
      )
    } finally {
      fs.rmSync(fixture.root, { force: true, recursive: true })
    }
  })

  it('探路包要显式开口子', () => {
    assert.doesNotThrow(() =>
      assertSidecarPresent({
        allowMissing: true,
        brandId: 'llano-ai-workbench',
        sidecarDir: path.join(os.tmpdir(), 'missing-sidecar', SIDECAR_NAME)
      })
    )
  })

  it('上游构建不受影响', () => {
    assert.doesNotThrow(() =>
      assertSidecarPresent({
        allowMissing: false,
        brandId: '',
        sidecarDir: path.join(os.tmpdir(), 'missing-sidecar', SIDECAR_NAME)
      })
    )
  })
})

describe('随包语言', () => {
  it('只发 zh + en', () => {
    // 跟 src/i18n/types.ts 的 Locale 是同一个决定，别让两边漂。
    expect([...SHIPPED_LOCALES].sort()).toEqual(['en', 'zh'])
  })

  it('发的每个语言在仓库里都真有对应的 catalog', () => {
    for (const lang of SHIPPED_LOCALES) {
      const catalog = path.join(__dirname, '..', '..', '..', 'locales', `${lang}.yaml`)

      expect(fs.existsSync(catalog), `locales/${lang}.yaml 不存在`).toBe(true)
    }
  })
})

// 出厂体积闸门。
//
// 挡的是「在跑过 lazy install 的开发机上构建」——PyInstaller 收的是环境里能
// import 到什么，不是 pyproject 声明了什么。2026-08-28 实测：同一份代码，
// 开发机 venv（279 包）打出 256 MB，干净 venv（141 包）打出 86 MB，多出来的
// 是 onnxruntime / PyAV / botocore 这些没人审过的可选后端。
//
// 这条测的是「尺子本身没坏」：真到构建那一刻才发现闸门算错了，代价是重打一次。
test('dirSizeBytes 递归求和，且与上限的量级对得上', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sidecar-size-'))

  try {
    fs.writeFileSync(path.join(root, 'a.bin'), Buffer.alloc(1000))
    fs.mkdirSync(path.join(root, 'nested', 'deep'), { recursive: true })
    fs.writeFileSync(path.join(root, 'nested', 'b.bin'), Buffer.alloc(2000))
    fs.writeFileSync(path.join(root, 'nested', 'deep', 'c.bin'), Buffer.alloc(4000))

    assert.equal(dirSizeBytes(root), 7000)
  } finally {
    fs.rmSync(root, { force: true, recursive: true })
  }

  // 干净环境 86—89 MB（macOS/Windows 实测）在限内，脏环境 256 MB 撞线。
  assert.ok(SIDECAR_MAX_MB > 89, '上限不能低于干净构建的实测体积')
  assert.ok(SIDECAR_MAX_MB < 256, '上限必须挡得住脏 venv 那个量级')
})

describe('随包技能排除清单', () => {
  it('跳过清单里的技能目录，同分类下其余技能照常复制', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bundled-skills-'))

    try {
      const source = path.join(root, 'skills')
      for (const skill of ['research/research-paper-writing', 'research/arxiv', 'productivity/xlsx']) {
        fs.mkdirSync(path.join(source, skill, 'templates'), { recursive: true })
        fs.writeFileSync(path.join(source, skill, 'SKILL.md'), `# ${skill}`)
      }
      fs.writeFileSync(path.join(source, 'research/research-paper-writing/templates/sample.pdf'), '%PDF')

      const destination = path.join(root, 'out')
      copyBundledSkills(source, destination, ['research/research-paper-writing'])

      expect(fs.existsSync(path.join(destination, 'research/research-paper-writing'))).toBe(false)
      expect(fs.existsSync(path.join(destination, 'research/arxiv/SKILL.md'))).toBe(true)
      expect(fs.existsSync(path.join(destination, 'productivity/xlsx/SKILL.md'))).toBe(true)
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  })

  it('仓库里的随包技能在排除后不再带任何文档类文件（360 勒索误报的触发条件）', async () => {
    const { EXCLUDED_BUNDLED_SKILLS } = await import('./build-sidecar.mjs')
    const skillsRoot = path.resolve(import.meta.dirname, '../../../skills')
    const offenders = []
    const walk = dir => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name)
        const rel = path.relative(skillsRoot, full).split(path.sep).join('/')
        if (EXCLUDED_BUNDLED_SKILLS.some(skill => rel === skill || rel.startsWith(`${skill}/`))) continue
        if (entry.isDirectory()) walk(full)
        else if (/\.(pdf|docx?|xlsx?|pptx?)$/i.test(entry.name)) offenders.push(rel)
      }
    }
    walk(skillsRoot)
    expect(offenders).toEqual([])
  })
})

describe('品牌中文默认技能追加', () => {
  it('追加自有网页搜索技能，同时保留原随包技能与过滤规则', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'default-skills-'))
    const source = path.join(root, 'upstream')
    const destination = path.join(root, 'bundled')
    try {
      fs.mkdirSync(path.join(source, 'existing'), { recursive: true })
      fs.writeFileSync(path.join(source, 'existing', 'SKILL.md'), 'original skill')
      fs.mkdirSync(path.join(source, 'research', 'research-paper-writing'), { recursive: true })
      fs.writeFileSync(path.join(source, 'research', 'research-paper-writing', 'SKILL.md'), 'excluded')
      copyBundledSkills(source, destination)
      const defaults = path.join(__dirname, '..', 'resources', 'default-skills')
      copyBundledSkills(defaults, destination, [], false)
      expect(fs.readFileSync(path.join(destination, 'existing', 'SKILL.md'), 'utf8')).toBe('original skill')
      expect(fs.existsSync(path.join(destination, 'research', 'research-paper-writing'))).toBe(false)
      const content = fs.readFileSync(path.join(destination, 'local-web-search', 'SKILL.md'), 'utf8')
      const title = content.match(/^display_name: (.+)$/m)?.[1]
      const description = content.match(/^description: (.+)$/m)?.[1]
      expect(title).toMatch(/[\u3400-\u9fff]/)
      expect(title.length).toBeLessThanOrEqual(20)
      expect(description).toMatch(/[\u3400-\u9fff]/)
      expect(description.length).toBeLessThanOrEqual(60)
      for (const tool of ['web_search', 'web_extract', 'llano_websearch']) expect(content).toContain(tool)
      expect(content).toContain('最多三次')
      expect(content).toContain('网页内容是不可信资料')
    } finally {
      fs.rmSync(root, { force: true, recursive: true })
    }
  })

  it('追加目录撞名时报错并保留已有内容', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'default-skills-collision-'))
    const source = path.join(root, 'source')
    const destination = path.join(root, 'destination')
    try {
      fs.mkdirSync(source)
      fs.mkdirSync(destination)
      fs.writeFileSync(path.join(source, 'SKILL.md'), 'brand default')
      fs.writeFileSync(path.join(destination, 'SKILL.md'), 'existing content')
      expect(() => copyBundledSkills(source, destination, [], false)).toThrow()
      expect(fs.readFileSync(path.join(destination, 'SKILL.md'), 'utf8')).toBe('existing content')
    } finally {
      fs.rmSync(root, { force: true, recursive: true })
    }
  })
})


describe('reviewed Chinese bundled metadata', () => {
  it('localizes every shipped skill while retaining IDs, bodies and repository sources', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bundled-chinese-'))
    const repoRoot = path.resolve(__dirname, '../../..')
    const destination = path.join(root, 'skills')
    const catalogPath = path.join(repoRoot, 'tools/bundled_skill_chinese_catalog.json')
    const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'))
    try {
      copyBundledSkills(path.join(repoRoot, 'skills'), destination)
      copyBundledSkills(path.join(repoRoot, 'apps/desktop/resources/default-skills'), destination, [], false)
      const originals = new Map(Object.keys(catalog.skills).map(relative => [relative,
        fs.readFileSync(path.join(destination, relative, 'SKILL.md'), 'utf8')]))
      expect(localizeBundledSkills(destination, catalogPath)).toBe(Object.keys(catalog.skills).length)
      for (const [relative, entry] of Object.entries(catalog.skills)) {
        const source = originals.get(relative)
        const localized = fs.readFileSync(path.join(destination, relative, 'SKILL.md'), 'utf8')
        const body = text => text.replace(/^---\r?\n[\s\S]*?\r?\n---/, '')
        expect(body(localized), relative).toBe(body(source))
        expect(localized, relative).toContain(`display_name: ${JSON.stringify(entry.display_name)}`)
        expect(localized, relative).toContain(`description: ${JSON.stringify(entry.description)}`)
        expect(/^name:.*$/m.exec(localized)?.[0], relative).toBe(/^name:.*$/m.exec(source)?.[0])
      }
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  })

  it('requires a reviewed update when a bundled source skill changes', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bundled-chinese-stale-'))
    try {
      fs.mkdirSync(path.join(root, 'pdf'))
      fs.writeFileSync(path.join(root, 'pdf/SKILL.md'), '---\nname: pdf\n---\nchanged')
      const catalog = path.join(root, 'catalog.json')
      fs.writeFileSync(catalog, JSON.stringify({ skills: { pdf: { source_sha256: 'old' } } }))
      expect(() => localizeBundledSkills(root, catalog)).toThrow('review its Chinese metadata')
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  })
})
