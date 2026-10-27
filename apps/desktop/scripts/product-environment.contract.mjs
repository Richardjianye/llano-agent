import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { stampExeIdentity } from './set-exe-identity.mjs'
import { spawnSync } from 'node:child_process'
import { test } from 'vitest'

import {
  desktopRoot,
  loadProductEnvironment,
  PRODUCT_ENVIRONMENTS,
  resolveProductEnvironment
} from './product-environment.mjs'

const EXPECTED_DISABLED_CAPABILITIES = [
  'nous_services',
  // 阶段 5 · U5：官方 git / GitHub ZIP / Nous 更新通道在品牌构建里必须不可达。
  // 自有更新闭环之后，能更新的只有公司清单那一条路。
  'self_update',
  'provider_management',
  'provider_oauth',
  'custom_model_providers',
  'moa',
  'builtin_image_generation',
  'pet_ai_generation'
]

test('branded policy publishes the same disabled capabilities to Desktop and managed YAML', () => {
  const policy = loadProductEnvironment({ LLANO_APP_ENV: 'development' })

  assert.deepEqual(policy.disabledCapabilities, EXPECTED_DISABLED_CAPABILITIES)

  const prepared = spawnSync(process.execPath, ['scripts/prepare-product-environment.mjs'], {
    cwd: desktopRoot,
    encoding: 'utf8',
    env: { ...process.env, LLANO_APP_ENV: 'development' }
  })
  assert.equal(prepared.status, 0, prepared.stderr || prepared.stdout)

  const yaml = fs.readFileSync(path.join(desktopRoot, 'build', 'managed-config', 'config.yaml'), 'utf8')
  const yamlCapabilities = yaml
    .split('\n')
    .slice(yaml.split('\n').findIndex(line => line.trim() === 'disabled_capabilities:') + 1)
    .filter(line => /^\s+-\s+/.test(line))
    .map(line => line.replace(/^\s+-\s+/, '').trim())
    .slice(0, EXPECTED_DISABLED_CAPABILITIES.length)

  assert.deepEqual(yamlCapabilities, EXPECTED_DISABLED_CAPABILITIES)

  // 界面默认语言走同一条路。漏在这儿的话现象是「全新安装是英文的」，而唯一能
  // 看出来的地方是员工装完之后的第一屏 —— 构建和测试都不会吭声。
  assert.equal(policy.defaultLanguage, 'zh')
  assert.match(yaml, /^ {2}default_language: zh$/m)
})

// 安装包的品牌身份（阶段 6）
//
// 这些字段决定用户看到什么：安装器标题、开始菜单快捷方式、控制面板卸载列表、
// exe 属性里的产品名。它们分散在 package.json 的 build 段和 rcedit 调用里，
// 而**产品名的真源只有 product-environments.json 一处** —— 下面这条把
// package.json 那几处钉回真源，免得改了配置、安装器上还是旧名字。
//
// 顺带扫一遍 build 段里还有没有 Hermes / Nous 的残留：改品牌时漏掉一两个字段
// 是常态（这次就漏在 dmg 标题、快捷方式名、卸载显示名和 Linux maintainer 上），
// 而这类残留只有装到机器上、点开"属性"才看得见。
test('安装包的品牌身份跟着 product-environments.json 走', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))
  const policy = loadProductEnvironment({ LLANO_APP_ENV: 'development' })

  assert.equal(pkg.build.productName, policy.productName)
  assert.equal(pkg.build.nsis.shortcutName, policy.productName)
  assert.equal(pkg.build.nsis.uninstallDisplayName, policy.productName)
  assert.equal(pkg.build.win.legalTrademarks, policy.productName)
})

test('build 段里不留 Hermes / Nous 的痕迹', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))
  const residue = JSON.stringify(pkg.build).match(/Hermes|Nous|nousresearch/gi)

  assert.equal(residue, null, `package.json build 段还留着上游身份：${residue?.join(', ')}`)
})

// Exercise the real stamping function while replacing only the Windows resource
// editor. This proves policy propagation on every host without requiring Wine.
test('exe 版本信息也从品牌配置读，不写死', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'llano-brand-contract-'))
  try {
    fs.mkdirSync(path.join(root, 'assets'))
    const icon = path.join(root, 'assets', 'icon.ico')
    fs.copyFileSync(path.join(desktopRoot, 'assets', 'icon.ico'), icon)
    const executable = path.join(root, 'Workbench.exe')
    fs.writeFileSync(executable, 'temporary resource editor target')
    const { version } = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))
    fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ version }))
    const configured = loadProductEnvironment({ LLANO_APP_ENV: 'development' })
    for (const policy of [configured, { ...configured, productName: '测试品牌工作台', companyName: '测试公司' }]) {
      const calls = []
      await stampExeIdentity(executable, root, { policy, edit: async (target, options) => { calls.push({ target, options }) } })
      assert.equal(calls.length, 1)
      const { target, options } = calls[0]
      assert.equal(target, executable)
      assert.equal(options.icon, icon)
      assert.equal(options['file-version'], version)
      assert.equal(options['product-version'], version)
      assert.deepEqual(options['version-string'], {
        ProductName: policy.productName,
        FileDescription: policy.productName,
        CompanyName: policy.companyName,
        LegalCopyright: `Copyright (c) ${new Date().getFullYear()} ${policy.companyName}`
      })
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }) }
})

// NSIS 卸载钩子（阶段 6 · 2026-08-25 上机实测测出来的两条残留）
//
// 这几条测的都是「生成出来的文本对不对」，因为这个宏在 macOS 上跑不了，而它
// 出错的方式是**静默的**：路径写错、宏名拼错、反斜杠多一层，NSIS 照样编译通过，
// 卸载时那两句什么也不做，只有装到机器上再卸掉才看得出来。
test('NSIS 卸载钩子会生成，且被 package.json 接上', () => {
  const prepared = spawnSync(process.execPath, ['scripts/prepare-product-environment.mjs'], {
    cwd: desktopRoot,
    encoding: 'utf8',
    env: { ...process.env, LLANO_APP_ENV: 'development' }
  })
  assert.equal(prepared.status, 0, prepared.stderr || prepared.stdout)

  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))
  assert.equal(pkg.build.nsis.include, 'build/installer.nsh')

  const nsh = fs.readFileSync(path.join(desktopRoot, 'build', 'installer.nsh'), 'utf8')
  const policy = loadProductEnvironment({ LLANO_APP_ENV: 'development' })

  // electron-builder 只认这一个宏名。拼错的话整个文件被静默忽略。
  assert.match(nsh, /!macro customUnInstall/)

  // 反斜杠只能有一层。NSIS 字符串里 `\` 不是转义符 —— 写成 `\\` 就是两个字面
  // 反斜杠，注册表路径直接不存在，而 NSIS 一声不吭（写这段时真踩过一次）。
  assert.doesNotMatch(nsh, /\\\\/)

  // 两个名字都必须跟品牌配置走
  assert.match(nsh, new RegExp(`DeleteRegKey HKCU "Software\\\\Classes\\\\${policy.deepLinkProtocol}"`))
  assert.ok(
    nsh.includes(`Delete "$APPDATA\\${policy.productName}\\product-session.json"`),
    'userData 路径没跟着 productName 走'
  )

  // 只在协议指向本次安装目录时才删 —— 装了两份时不该互相干掉
  assert.match(nsh, /\$\{If\} \$0 == '"\$INSTDIR\\/)

  // ⚠️ 清凭据必须包在 ${ifNot} ${isUpdated} 里。
  //
  // NSIS 在装新版之前会静默跑一遍旧版卸载器，所以这个宏在**更新路径上也会执行**。
  // 不加这层判断的后果（2026-08-28 实测）：用户每更新一次掉一次登录，而且旧的
  // 设备 API Key **永远不会被撤销** —— 删文件这条路上没有任何网络调用，连
  // product-revocation-pending.json（专门用来事后补撤销的墓碑）也被一起删了。
  // 结果是每更新一次，网关上多一把永久有效的孤儿密钥。
  const guarded = nsh.slice(nsh.indexOf('${ifNot} ${isUpdated}'), nsh.indexOf('${endIf}\n!macroend'))
  assert.ok(nsh.includes('${ifNot} ${isUpdated}'), '清凭据没有包在 isUpdated 判断里')
  assert.ok(guarded.includes('product-session.json'), 'product-session.json 不在 isUpdated 保护范围内')
  assert.ok(
    guarded.includes('product-revocation-pending.json'),
    '撤销墓碑不在 isUpdated 保护范围内 —— 它被删掉就再也补不回那次撤销'
  )
})

// 仓库里那份真配置：**已经填上**的每个环境都必须能真的解析得动。
// 这条用例的作用是让「补一个环境条目」这件事本身有人验 —— 复制 development
// 却漏改一行（阶段 6 补 test 时的现实风险）会直接在这里红，而不是等到装完
// 才发现客户端连的是 127.0.0.1。
//
// 不去断言「哪些环境缺着」：那是一个会随部署进度变化的事实，不是不变量。
// 缺失时必须硬失败这条不变量由下面的内存用例钉，那条永远不会因为条目补齐
// 而变成空转。
test('每个已填写的环境条目都要真的解析得动', () => {
  const configured = Object.keys(JSON.parse(fs.readFileSync(path.join(desktopRoot, 'product-environments.json'), 'utf8')).environments)

  assert.ok(configured.length > 0, 'product-environments.json 里一个环境都没有')

  for (const environment of configured) {
    assert.ok(PRODUCT_ENVIRONMENTS.includes(environment), `配置里有个不认识的环境：${environment}`)

    const policy = loadProductEnvironment({ LLANO_APP_ENV: environment })

    assert.equal(policy.environment, environment)
    assert.ok(policy.gatewayOrigin.startsWith('http'))
    assert.ok(policy.modelApiBaseUrl.startsWith(`${policy.gatewayOrigin}/`))
  }
})

// 条目缺失走的是上面那条早退，`requiredUrl` 的空值判断根本到不了 —— 只测缺失
// 会让「空地址兜个默认值」这种变异活下来（实测活过一次）。填了一半的条目才是
// 唯一能走到那道判断的路径，而它也是现实里更容易发生的那种：补 test 环境时
// 复制了 development 却漏改其中一行。
// 每格都带上**期望的报错**，不只是「抛了」。原因是这两道判断会互相遮蔽：
// 把「地址空着」改成兜一个默认值之后，它照样会被下面「必须挂在网关下」那道
// 拦住 —— 只断言 throws 的话变异不报红（实测活过两次）。而两者的诊断方向
// 完全相反：一个是「这一行没填」，一个是「这一行填到别的域名去了」。
const PARTIAL_CASES = [
  ['网关地址空着', { gatewayOrigin: '' }, /missing gatewayOrigin/],
  ['模型地址空着', { modelApiBaseUrl: '' }, /missing modelApiBaseUrl/],
  ['清单地址空着', { updateManifestUrl: '' }, /missing updateManifestUrl/],
  ['公钥空着', { updateSigningPublicKey: '' }, /missing updateSigningPublicKey/],
  ['公钥不是 32 字节', { updateSigningPublicKey: 'c2hvcnQ=' }, /32-byte Ed25519/],
  [
    '模型地址挂在别的域名下',
    { modelApiBaseUrl: 'https://elsewhere.example.com/v1' },
    /modelApiBaseUrl must be hosted below gatewayOrigin/
  ],
  [
    '清单地址挂在别的域名下',
    { updateManifestUrl: 'https://elsewhere.example.com/v1/client-update' },
    /updateManifestUrl must be hosted below gatewayOrigin/
  ]
]

const rawWith = (overrides = {}) => ({
  brandId: 'llano-ai-workbench',
  deepLinkProtocol: 'llano-agent',
  productName: 'LLANO Agent',
  companyName: 'LLANO Agent',
  providerId: 'llano-gateway',
  providerName: '公司 AI 网关',
  defaultLanguage: 'zh',
  environments: {
    test: {
      gatewayOrigin: 'https://gw.example.com',
      modelApiBaseUrl: 'https://gw.example.com/v1',
      updateManifestUrl: 'https://gw.example.com/v1/client-update',
      updateSigningPublicKey: '1AaTJvXt1VYX/GIPusE4piA71VqMx5wJbQAikeU6x+k=',
      ...overrides
    }
  }
})

// 基线放在前面，因为它挡住的是一次真实的假绿：最早写的 fixture 漏了品牌那几个
// 顶层字段，于是七条用例**全都**是在 deepLinkProtocol 上抛的，与各自要测的那个
// 字段毫无关系 —— 七条全绿，七条都没测到东西。
test('基线：四个字段都对时确实能过（否则下面七条可能全是假绿）', () => {
  assert.equal(resolveProductEnvironment(rawWith(), 'test').gatewayOrigin, 'https://gw.example.com')
})

// 产品名 / 公司名空着也要挡住构建。这两个是顶层字段，走不到上面那七条
// 环境级用例里，而它们同样会被烧进 exe 版本信息和 Info.plist —— rcedit 对空
// 字符串一声不吭地照写，成品属性页一片空白，构建日志里什么都没有。
for (const field of ['productName', 'companyName']) {
  test(`品牌顶层字段空着必须挡住构建：${field}`, () => {
    assert.throws(() => resolveProductEnvironment({ ...rawWith(), [field]: '' }, 'test'), new RegExp(`Product ${field} is missing`))
  })
}

// 界面默认语言写错一个字母，后果是「配了中文，装出来还是英文」——
// normalizeLocale 对认不出来的值一律静默落回英文，构建全程不报一个字。
for (const bad of ['', 'zh-CN', 'ZH', 'chinese', undefined]) {
  test(`defaultLanguage 不是发出去的那两个之一就挡住构建：${JSON.stringify(bad)}`, () => {
    assert.throws(
      () => resolveProductEnvironment({ ...rawWith(), defaultLanguage: bad }, 'test'),
      /defaultLanguage must be one of en, zh/
    )
  })
}

// 环境条目整个缺失时必须硬失败，而且报错要说清楚补什么。
//
// 真正怕的是反过来：某天有人加个 `|| 'http://localhost'` 之类的兜底，于是一个
// 指向空地址的安装包安安静静地发了出去，装上的人看到的是「连不上网关」，
// 没人会想到是打包时选错了环境。
//
// 喂内存对象而不是读真配置：读真配置的话，等 test / production 都填齐了这条
// 就会变成空转的假绿 —— 而它要钉的那道早退跟「现在哪些环境填了」没有关系。
for (const environment of PRODUCT_ENVIRONMENTS) {
  test(`环境条目整个缺失时必须挡住构建：${environment}`, () => {
    assert.throws(
      () => resolveProductEnvironment({ ...rawWith(), environments: {} }, environment),
      error => {
        assert.match(error.message, new RegExp(environment))
        // 报错必须指到这个文件、并点名缺的字段，否则读者只知道「构建挂了」
        assert.match(error.message, /product-environments\.json/)
        assert.match(error.message, /gatewayOrigin/)
        assert.match(error.message, /updateSigningPublicKey/)

        return true
      }
    )
  })
}

for (const [label, overrides, expected] of PARTIAL_CASES) {
  test(`填了一半的环境条目也必须挡住构建：${label}`, () => {
    assert.throws(() => resolveProductEnvironment(rawWith(overrides), 'test'), expected)
  })
}

// 构建物分发点白名单（artifactOrigins）
//
// 生产网关在 Zeabur，安装包在内网分发机上 —— 两者本来就不同源，所以
// update-download.ts 那道「必须与网关同源」被放宽成了一份编译期列表。
// 下面这几条钉的是：列表仍然由构建决定、写错会挡住构建、而**没写的环境
// 行为跟放宽之前一模一样**（后者是最容易在重构里悄悄丢掉的那条）。

test('没写 artifactOrigins 的环境只认网关自己', () => {
  const policy = resolveProductEnvironment(rawWith(), 'test')

  assert.deepEqual(policy.artifactOrigins, ['https://gw.example.com'])
})

test('网关自己永远在列，不用也不必重复写', () => {
  const policy = resolveProductEnvironment(
    rawWith({ artifactOrigins: ['https://gw.example.com', 'http://192.0.2.130'] }),
    'test'
  )

  // 重复写不会出现两次，网关始终排第一
  assert.deepEqual(policy.artifactOrigins, ['https://gw.example.com', 'http://192.0.2.130'])
})

// 每格都带上期望的报错。只断言 throws 的话，这几道判断会互相遮蔽 ——
// 一个带路径的条目在「必须是 http(s)」那道也过得去，诊断方向却完全不同。
const BAD_ARTIFACT_ORIGINS = [
  ['不是数组', 'http://192.0.2.130', /artifactOrigins must be an array/],
  ['条目是空串', [''], /artifactOrigins has an empty entry/],
  ['条目不是地址', ['192.0.2.130'], /artifactOrigins has an invalid origin/],
  ['条目用了别的协议', ['ftp://192.0.2.130'], /artifactOrigins entries must use http:\/\/ or https:\/\//],
  // 带路径的条目必须报错而不是被静默截断：写了 /releases 却以为限定了目录，
  // 是这份配置最容易骗人的地方 —— 放行的从来是「哪台机器」。
  ['条目带路径', ['http://192.0.2.130/releases'], /must be bare origins/]
]

for (const [label, value, expected] of BAD_ARTIFACT_ORIGINS) {
  test(`artifactOrigins 写错必须挡住构建：${label}`, () => {
    assert.throws(() => resolveProductEnvironment(rawWith({ artifactOrigins: value }), 'test'), expected)
  })
}

// 仓库里那份真配置：生产环境的分发点必须是**内网地址**。
//
// 这条挡的是「顺手把某个公网 CDN 加进去」—— 那等于把「客户端只访问公司
// 构建物」这条产品规矩作废，而白名单的形式让它看上去仍然受控。
test('生产环境的额外分发点只能是内网地址', () => {
  const policy = loadProductEnvironment({ LLANO_APP_ENV: 'production' })

  assert.equal(policy.artifactOrigins[0], policy.gatewayOrigin, '网关必须排第一')

  for (const origin of policy.artifactOrigins.slice(1)) {
    const host = new URL(origin).hostname

    assert.match(
      host,
      /^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/,
      `生产分发点 ${origin} 不是内网地址 —— 安装包不该挂在公网上`
    )
  }
})

// 运行期 pip 的两道闩（阶段 6 · S6-E）
//
// 上游 tools/lazy_deps.py 默认 allow_lazy_installs=true —— 可选后端第一次被用到
// 时会现场从 PyPI 装包。闸门本身还在不在，由 Python 侧
// tests/tools/test_branded_lazy_install_lockdown.py 盯着；这里盯的是**我们有没有
// 真的去拉那个闸**。两边分开测，因为它们会各自独立地坏掉。
test('自有 skill hub 的位置留着，且允许为空', () => {
  // 空串是「先留位置」而不是「忘了填」—— hub 上线当天客户端就有落点。
  // 真源里没有这个键的话，下游 product_policy 读到的是 undefined 而不是空串。
  const policy = loadProductEnvironment({ LLANO_APP_ENV: 'development' })

  assert.equal(typeof policy.skillHubUrl, 'string')

  const yaml = fs.readFileSync(path.join(desktopRoot, 'build', 'managed-config', 'config.yaml'), 'utf8')

  assert.match(yaml, /^ {2}skill_hub_url:/m)
})

test('生成的 managed config 关掉运行期 pip', () => {
  const prepared = spawnSync(process.execPath, ['scripts/prepare-product-environment.mjs'], {
    cwd: desktopRoot,
    encoding: 'utf8',
    env: { ...process.env, LLANO_APP_ENV: 'development' }
  })
  assert.equal(prepared.status, 0, prepared.stderr || prepared.stdout)

  const yaml = fs.readFileSync(path.join(desktopRoot, 'build', 'managed-config', 'config.yaml'), 'utf8')

  assert.match(yaml, /^security:\n {2}allow_lazy_installs: false$/m)
})

test('后端环境变量里带着第二道闩', () => {
  // managed config 解析失败是 fail-open 的（managed_scope._cached_read 只记一条
  // warning），主闸那时候不设防，所以这条不是重复。
  // 读源码前先剥注释 —— 断言命中自己写的说明是本轮踩过的坑。
  const source = fs
    .readFileSync(path.join(desktopRoot, 'electron', 'main.ts'), 'utf8')
    .split('\n')
    .filter(line => !line.trim().startsWith('//'))
    .join('\n')
  const start = source.indexOf('function productBackendEnv(')
  const end = source.indexOf('\nfunction productSessionPath(', start)

  assert.ok(start > 0 && end > start, 'productBackendEnv 没找到')
  assert.match(source.slice(start, end), /HERMES_DISABLE_LAZY_INSTALLS: '1'/)

  // 配了 target 的话 =1 就变成改道而不是封死（见 lazy_deps._allow_lazy_installs）。
  assert.doesNotMatch(source, /HERMES_LAZY_INSTALL_TARGET/)
})

// 上游品牌资源不许回到 public/。
//
// 这条挡的是**月度 upstream 同步**：`nous-girl.jpg` / `hermes.png` /
// `hermes-sprite.png` 是上游仓库里的文件，同步一次就会被带回来，而它们会
// 直接出现在用户眼前（BrandMark 用在关于页、更新提示、首次运行、安装引导）。
//
// 2026-08-28 之前它们一直在包里：nous-girl 被 BrandMark 渲染着，另外两个
// 零引用但照样进 asar，双份（/dist 和 /public 各一），合计 4.6 MB。
// 「修完安装包残留 Hermes/Nous 元数据」那一轮没查到它们 —— 那轮查的是
// 元数据字段，不是图片资源。
test('public/ 里不许出现上游品牌图片', () => {
  const publicDir = path.join(desktopRoot, 'public')
  const banned = fs
    .readdirSync(publicDir)
    .filter(name => /^(hermes|nous)[-.]/i.test(name))

  assert.deepEqual(
    banned,
    [],
    `上游品牌资源回到了 public/（多半是月度同步带回来的）：\n${banned.join('\n')}\n` +
      '产品自己的品牌标记是 brand-mark.png，见 src/components/brand-mark.tsx'
  )
})
