/**
 * 模板里不能写 Markdown —— 页面不渲染它。
 *
 * ## 为什么会踩这个坑
 *
 * 这些页面的说明文字是当作「文档」写的，写文档时强调一句话的习惯动作就是打两个
 * 星号。但 `<v-card-subtitle>` / `<v-alert>` 的插槽是**普通 HTML**，没有任何一层
 * 会把 `**x**` 变成粗体 —— 页面上原样显示两串星号。
 *
 * 这和 layout-classes.test.ts 钉的是同一类问题：**写上去不报错**。编译过、lint 过、
 * 类型过，只有真的打开页面看才会发现。而说明文字恰恰是最不会有人逐字看的地方，
 * 星号能在那儿挂很久。2026-08-31「屏蔽词」「技能库」两页就是这么发出去的。
 *
 * 要加粗就用 `<b>`：模板里它是一个真的元素，不是待解析的标记。
 *
 * 但**只在插槽里**如此。写在 `hint=` / `text=` 这类 prop 上的文案是当纯字符串渲染的，
 * `<b>` 在那儿会原样显示成尖括号 —— 那种位置只能把标记去掉，不能换成标签。
 * 这条守卫拦不住它（它只认 `**`），靠的是写的时候知道。
 *
 * 注释里的 `**` 不算 —— 那是给读代码的人看的，不进 DOM。
 */

import { describe, expect, it } from 'vitest'

const PAGES = import.meta.glob('./*.vue', { eager: true, query: '?raw', import: 'default' }) as Record<string, string>

/** 取 `<template>` 块：`<script` 之前的全部内容，再去掉 HTML 注释 */
function renderedTemplate (source: string): string {
  const scriptAt = source.indexOf('<script')
  return (scriptAt === -1 ? source : source.slice(0, scriptAt))
    .replaceAll(/<!--.*?-->/gs, '')
}

describe('模板文案', () => {
  it('不用 Markdown 加粗（页面不渲染 md，要加粗写 <b>）', () => {
    const offenders: string[] = []

    for (const [path, source] of Object.entries(PAGES)) {
      for (const match of renderedTemplate(source).matchAll(/\*\*[^\n*]+\*\*/g)) {
        offenders.push(`${path}: ${match[0]}`)
      }
    }

    expect(offenders).toEqual([])
  })

  it('扫到了页面（否则上面那条永远是绿的）', () => {
    // 与 layout-classes.test.ts 同样的反向基线：glob 路径写错时 PAGES 是空对象，
    // 空数组当然等于空数组，守卫会永远绿着。
    expect(Object.keys(PAGES).length).toBeGreaterThan(5)
  })
})
