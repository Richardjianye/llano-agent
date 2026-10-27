import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const desktopRoot = path.resolve(here, '..')
const configPath = path.join(desktopRoot, 'product-environments.json')

export const PRODUCT_ENVIRONMENTS = ['development', 'test', 'production']

export function selectedProductEnvironment(env = process.env) {
  const selected = String(env.LLANO_APP_ENV || 'development')
    .trim()
    .toLowerCase()

  if (!PRODUCT_ENVIRONMENTS.includes(selected)) {
    throw new Error(
      `Unsupported LLANO_APP_ENV=${JSON.stringify(selected)}. Expected one of: ${PRODUCT_ENVIRONMENTS.join(', ')}.`
    )
  }

  return selected
}

function requiredUrl(value, field, environment) {
  const normalized = String(value || '')
    .trim()
    .replace(/\/+$/, '')

  if (!normalized) {
    throw new Error(
      `Product environment ${environment} is missing ${field} in ${configPath}. ` +
        'Fill the fixed deployment address before building this environment.'
    )
  }

  let parsed

  try {
    parsed = new URL(normalized)
  } catch {
    throw new Error(`Product environment ${environment} has an invalid ${field}: ${JSON.stringify(value)}.`)
  }

  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error(`Product environment ${environment} ${field} must use http:// or https://.`)
  }

  return normalized
}

function requiredSigningKey(value, environment) {
  const normalized = String(value || '').trim()

  if (!normalized) {
    throw new Error(
      `Product environment ${environment} is missing updateSigningPublicKey in ${configPath}. ` +
        'Generate the long-lived key pair on the offline signing machine ' +
        '(audit-gateway/release/sign_release.py keygen) and paste the .public value here. ' +
        'A build with no trust root can never install an update.'
    )
  }

  // 必须是 32 字节 Ed25519 原始公钥的 base64。在这里卡住而不是等运行时验签失败：
  // 一把长度不对的公钥会让**所有**更新验签失败，而现象是「签名验证不通过」，
  // 第一反应是去查签名机，不是去查这一行配置。
  let raw

  try {
    raw = Buffer.from(normalized, 'base64')
  } catch {
    throw new Error(`Product environment ${environment} updateSigningPublicKey is not valid base64.`)
  }

  if (raw.length !== 32 || raw.toString('base64') !== normalized) {
    throw new Error(
      `Product environment ${environment} updateSigningPublicKey must be a base64 32-byte Ed25519 ` +
        `public key, got ${raw.length} bytes.`
    )
  }

  return normalized
}

// 界面默认语言。真源是 src/i18n/languages.ts 的 LOCALE_OPTIONS；这里只认它发出
// 去的那两个，写错一个字母就报错 —— 否则 normalizeLocale 会把 "zh-CN "、"Zh"
// 这类值静默落回英文，而现象是「配了中文但装出来还是英文」，最难查的那种。
const PRODUCT_LOCALES = ['en', 'zh']

function requiredLocale(value) {
  const normalized = String(value ?? '').trim()

  if (!PRODUCT_LOCALES.includes(normalized)) {
    throw new Error(
      `Product defaultLanguage must be one of ${PRODUCT_LOCALES.join(', ')}, got ${JSON.stringify(value)}.`
    )
  }

  return normalized
}

function requiredName(value, field) {
  const normalized = String(value ?? '').trim()

  // 这两个名字会被烧进 Windows exe 的版本信息和 macOS 的 Info.plist，装到
  // 用户机器上之后再改就得重推安装包。空着的话 rcedit 会照样写一个空字符串
  // 进去，成品的「属性 → 详细信息」里是一片空白，而构建全程不报一个字。
  if (!normalized) {
    throw new Error(`Product ${field} is missing in ${configPath}.`)
  }

  return normalized
}

function requiredProtocol(value) {
  const normalized = String(value || '').trim().toLowerCase()

  if (!/^[a-z][a-z0-9+.-]*$/.test(normalized)) {
    throw new Error(`Product deepLinkProtocol must be a valid URI scheme, got ${JSON.stringify(value)}.`)
  }

  return normalized
}

/**
 * 构建物可以放在哪些 origin 上。
 *
 * **网关自己永远在列**，不用写。额外条目是给「安装包不由网关分发」这件事留的：
 * 生产网关在 Zeabur，而一份 180 MB 的安装包不该让全公司绕一趟公网去拿，
 * 何况 Zeabur 的直传上限只有 100 MB —— 那道限制根本不该由客户端来承担。
 *
 * 放宽的是**去哪儿拿字节**，不是**信谁**。信任根仍然只有两样：清单的 Ed25519
 * 签名，和落地字节的 SHA-256（见 update-download.ts 文件头「三道校验分别防
 * 什么」）。托管机被控制也换不掉内容 —— 换了就对不上哈希，下载中止，旧版本
 * 一个字节没动。
 *
 * 只收 origin（协议 + 主机 + 端口），带路径的条目**直接报错而不是静默截断**：
 * 写了 `http://host/releases` 却以为自己限定了目录，是这种配置最容易骗人的
 * 地方 —— 允许的从来是「哪台机器」。
 */
function optionalArtifactOrigins(value, environment, gatewayOrigin) {
  const allowed = [gatewayOrigin]

  if (value === undefined || value === null) {
    return Object.freeze(allowed)
  }

  if (!Array.isArray(value)) {
    throw new Error(
      `Product environment ${environment} artifactOrigins must be an array of http(s) origins.`
    )
  }

  for (const entry of value) {
    const normalized = String(entry ?? '').trim().replace(/\/+$/, '')

    if (!normalized) {
      throw new Error(`Product environment ${environment} artifactOrigins has an empty entry.`)
    }

    let parsed

    try {
      parsed = new URL(normalized)
    } catch {
      throw new Error(
        `Product environment ${environment} artifactOrigins has an invalid origin: ${JSON.stringify(entry)}.`
      )
    }

    if (!['http:', 'https:'].includes(parsed.protocol)) {
      throw new Error(`Product environment ${environment} artifactOrigins entries must use http:// or https://.`)
    }

    if (parsed.origin !== normalized) {
      throw new Error(
        `Product environment ${environment} artifactOrigins entries must be bare origins ` +
          `(scheme + host + port). Got ${JSON.stringify(entry)}, write ${JSON.stringify(parsed.origin)} instead.`
      )
    }

    if (!allowed.includes(parsed.origin)) {
      allowed.push(parsed.origin)
    }
  }

  return Object.freeze(allowed)
}

/**
 * 校验并冻结一个环境的配置。与读文件分开，是为了让「条目填了一半」这种情形
 * 能被直接测到 —— 它只能靠喂一个内存里的对象来构造，而不是去改仓库里那份
 * 真配置（测试崩在中途就会把它留成坏的）。
 */
export function resolveProductEnvironment(raw, environment) {
  const selected = raw.environments?.[environment]

  // 未定下来的环境这里**没有条目**，不是留一个空串占位的条目。空串条目
  // 一眼扫过去像「已经配好了」，而这个文件唯一绝不能干的事就是让一个指向
  // 空地址的包发出去。缺条目时直接说清楚要补什么，比事后猜一个空串在哪。
  if (!selected) {
    throw new Error(
      `Product environment ${environment} is not configured in ${configPath}. ` +
        `Add an "${environment}" entry with gatewayOrigin, modelApiBaseUrl, updateManifestUrl and ` +
        'updateSigningPublicKey — see the "development" entry for the shape.'
    )
  }

  const gatewayOrigin = requiredUrl(selected.gatewayOrigin, 'gatewayOrigin', environment)
  const modelApiBaseUrl = requiredUrl(selected.modelApiBaseUrl, 'modelApiBaseUrl', environment)
  const updateManifestUrl = requiredUrl(selected.updateManifestUrl, 'updateManifestUrl', environment)
  const updateSigningPublicKey = requiredSigningKey(selected.updateSigningPublicKey, environment)
  const artifactOrigins = optionalArtifactOrigins(selected.artifactOrigins, environment, gatewayOrigin)

  if (!modelApiBaseUrl.startsWith(`${gatewayOrigin}/`)) {
    throw new Error(`Product environment ${environment} modelApiBaseUrl must be hosted below gatewayOrigin.`)
  }

  // 清单也必须挂在网关下面。清单地址指到别处 = 有人能在不碰网关的情况下
  // 决定客户端去问谁要更新，而那正是签名要防的那件事的前半段。
  if (!updateManifestUrl.startsWith(`${gatewayOrigin}/`)) {
    throw new Error(`Product environment ${environment} updateManifestUrl must be hosted below gatewayOrigin.`)
  }

  return Object.freeze({
    allowedMessagingPlatforms: Object.freeze([...(raw.allowedMessagingPlatforms || [])]),
    artifactOrigins,
    brandId: String(raw.brandId),
    companyName: requiredName(raw.companyName, 'companyName'),
    // 全新安装时的界面语言。不按环境分 —— 品牌是哪国的跟连哪个网关无关。
    defaultLanguage: requiredLocale(raw.defaultLanguage),
    managedImageGeneration: raw.managedImageGeneration === true,
    syncSkillUsage: raw.syncSkillUsage === true,
    deepLinkProtocol: requiredProtocol(raw.deepLinkProtocol),
    disabledCapabilities: Object.freeze([...(raw.disabledCapabilities || [])]),
    environment,
    gatewayOrigin,
    modelApiBaseUrl,
    productName: requiredName(raw.productName, 'productName'),
    providerId: String(raw.providerId),
    providerName: String(raw.providerName),
    // 自有 skill hub 的地址。留空 = 还没上线，那时品牌构建一个远程技能源都没有
    // （见 tools/skills_hub.py 的 PRODUCT_SKILL_SOURCE_IDS）。空串是合法值，
    // 所以这里不能用 requiredName —— 它是「先留位置」，不是「忘了填」。
    skillHubUrl: String(raw.skillHubUrl ?? ''),
    updateManifestUrl,
    updateSigningPublicKey
  })
}

export function loadProductEnvironment(env = process.env) {
  return resolveProductEnvironment(JSON.parse(fs.readFileSync(configPath, 'utf8')), selectedProductEnvironment(env))
}

export { configPath, desktopRoot }
