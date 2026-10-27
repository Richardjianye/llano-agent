/**
 * 布局类必须用 Tailwind，不能用 Vuetify 专有的那套。
 *
 * ## 为什么会踩这个坑
 *
 * 这个项目同时装了 Vuetify 和 Tailwind，但**没有引入 Vuetify 的工具类样式**。
 * 于是 `d-flex` / `flex-column` / `ga-4` / `pa-4` 写上去**不报任何错**，
 * 编译过、lint 过、类型过，页面渲染出来只是——没有布局。
 *
 * 更阴的是 `mb-4` / `mt-4` **两边同名**，所以随手写几个间距类看起来是work的，
 * 让人以为整套都能用。2026-08-31 三个新页面就是这么写出来的：负责人打开一看
 * 「组件之间没有间隔」。
 *
 * `gap-4` 这类没出现在构建产物里，不是因为它不可用，而是 Tailwind 是 JIT ——
 * 源码里没人写过就不生成。所以「CSS 里搜不到」不能用来判断可不可用，
 * 只能从**类名属于哪一套**来判断。
 */

import { describe, expect, it } from 'vitest'

/** Vuetify 专有、这个项目里**不生效**的布局类 */
const VUETIFY_ONLY = [
  /\bd-flex\b/,
  /\bd-block\b/,
  /\bd-none\b/,
  /\bflex-column\b/,
  /\bflex-grow-1\b/,
  /\bflex-shrink-0\b/,
  /\bga-\d\b/,
  /\bpa-\d\b/,
  /\bma-\d\b/,
  /\balign-center\b/,
  /\bjustify-space-between\b/,
]

// 用 Vite 的 glob 读源码，不用 node:fs —— src/ 的 tsconfig 没有 node 类型，
// 而为一条守卫用例把 node 类型引进整个 src 项目是本末倒置。
const PAGES = import.meta.glob('./*.vue', { eager: true, query: '?raw', import: 'default' }) as Record<string, string>

describe('页面布局类', () => {
  it('不使用 Vuetify 专有的布局类（这个项目只装了 Tailwind 的工具类）', () => {
    const offenders: string[] = []

    for (const [path, source] of Object.entries(PAGES)) {
      // 只看 class 属性里的内容，注释里提到类名不算。
      for (const match of source.matchAll(/class="([^"]*)"/g)) {
        for (const pattern of VUETIFY_ONLY) {
          if (pattern.test(match[1])) {
            offenders.push(`${path}: class="${match[1]}" 命中 ${pattern}`)
          }
        }
      }
    }

    expect(offenders).toEqual([])
  })

  it('扫到了页面（否则上面那条永远是绿的）', () => {
    // 反向基线。glob 写错路径的话 PAGES 是空对象，而空数组当然等于空数组 ——
    // 一条永远不会红的守卫比没有守卫更糟，它会让人以为这件事被看着。
    expect(Object.keys(PAGES).length).toBeGreaterThan(5)
  })
})
