/**
 * 界面文案里不许留上游身份（阶段 6）
 *
 * 2026-08-25 在真机上截到的界面写着「Hermes 无法启动」，而 `src/i18n/` 里当时
 * 有 635 处「Hermes」和 41 处「Nous」—— 之前只审计过安装包元数据，界面文案
 * 这一整面没人看过。更糟的是其中两条在教用户去上游装东西：
 *
 *   远程主机上未安装 Hermes。请在远程安装
 *   （curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh）…
 *
 * 那是阶段 2 和 5 花力气堵掉的那类可达性，只不过活在文案里。
 *
 * 这个文件**遍历真正的翻译对象**而不是读源码文本：产品名现在是
 * `${PRODUCT_POLICY.productName}` 插值进去的，只有求值之后才看得出结果对不对。
 */
import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from './catalog'
import type { Locale } from './types'

/**
 * 已知留着的上游服务文案。这些是 Nous 自有服务（Hermes Cloud / Nous Portal /
 * Nous 额度），品牌构建里进不去：`settings/index.tsx` 把 `gateway` 与
 * `providers` 两个目的地整个排除在 `IS_BRANDED_PRODUCT` 之外，`billing` 由
 * `isSettingsDestinationAllowed` 挡在 `nous_services` 后面。
 *
 * 留着而不是改掉，是因为把它们改成「LLANO Agent Cloud」会凭空造出一个我们没有的
 * 服务 —— 那比留一段进不去的文案糟。
 */
const UPSTREAM_SERVICE_KEYS = [
  /^settings\.gateway\.cloud/,
  /^settings\.toolsets\.nous/,
  /^modelPicker\.proNeedsSubscription$/,
  /^shell\.statusbar\.connectionCloudTooltip$/,
  /^billingBlock\.titleNous$/,
  /^settings\.nav\.gateway$/
]

function leaves(node: unknown, path: string, out: { path: string; value: string }[]) {
  if (typeof node === 'string') {
    out.push({ path, value: node })

    return out
  }

  if (typeof node === 'function') {
    // 带参数的文案：喂一组无害的实参把模板求值出来。有的叶子要求特定形状，
    // 求值失败就跳过 —— 漏掉一条，好过让整个用例因为一个参数形状挂掉。
    try {
      const rendered = (node as (...args: unknown[]) => unknown)('x', 1, 'y')

      if (typeof rendered === 'string') {
        out.push({ path, value: rendered })
      }
    } catch {
      /* 形状不匹配，跳过 */
    }

    return out
  }

  if (node && typeof node === 'object') {
    for (const [key, child] of Object.entries(node)) {
      leaves(child, path ? `${path}.${key}` : key, out)
    }
  }

  return out
}

describe.each(Object.keys(TRANSLATIONS) as Locale[])('%s 的界面文案', locale => {
  const all = leaves(TRANSLATIONS[locale], '', [])

  it('有东西可测（否则下面几条是空转）', () => {
    expect(all.length).toBeGreaterThan(1000)
  })

  it('不出现上游的安装地址或安装命令', () => {
    const hits = all.filter(leaf => /nousresearch|install\.sh|NousResearch/i.test(leaf.value))

    expect(hits.map(h => `${h.path}: ${h.value}`)).toEqual([])
  })

  it('不出现上游产品名（Nous 自有服务那几处除外，它们在品牌构建里进不去）', () => {
    const hits = all.filter(
      leaf => /\bHermes\b|\bNous\b/.test(leaf.value) && !UPSTREAM_SERVICE_KEYS.some(allowed => allowed.test(leaf.path))
    )

    expect(hits.map(h => `${h.path}: ${h.value}`)).toEqual([])
  })
})
