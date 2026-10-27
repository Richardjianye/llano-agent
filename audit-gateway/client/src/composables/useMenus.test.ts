/**
 * 钉住一条不变式：路由声明了 `meta.menu`，它就必须在 MENU_ORDER 里。
 *
 * 违反它的表现是「页面在、URL 敲进去能打开、菜单里没有」，没有任何报错。
 * 这个坑踩过两次（releases、skill-events），所以用例直接对着路由表比。
 *
 * 这里 import 的是 `@/router/routes` 而不是 `@/router` —— 后者一 import
 * 就会执行 `createWebHistory()`，在 node 环境下要摸 document，跑不起来。
 */

import type { RouteRecordRaw } from 'vue-router'
import { describe, expect, it } from 'vitest'
import { MENU_ORDER } from '@/composables/useMenus'
import { routes } from '@/router/routes'

function menuRouteNames (records: readonly RouteRecordRaw[]): string[] {
  return records.flatMap(record => [
    ...(record.meta?.menu && record.name ? [String(record.name)] : []),
    ...menuRouteNames(record.children ?? []),
  ])
}

describe('MENU_ORDER', () => {
  it('列全了所有 meta.menu 的路由', () => {
    const declared = menuRouteNames(routes)
    expect(declared.length).toBeGreaterThan(0)

    // 断言「差集为空」而不是比两个排序后的数组：失败信息直接就是
    // 「这几个路由声明了 menu 却不在 MENU_ORDER 里」，不用再自己对着两列找。
    const missing = declared.filter(name => !MENU_ORDER.includes(name))
    expect(missing).toEqual([])
  })

  it('没有指向不存在路由的名字', () => {
    const allNames = new Set<string>()
    const walk = (records: readonly RouteRecordRaw[]) => {
      for (const record of records) {
        if (record.name) {
          allNames.add(String(record.name))
        }
        walk(record.children ?? [])
      }
    }
    walk(routes)

    expect(MENU_ORDER.filter(name => !allNames.has(name))).toEqual([])
  })
})

describe('未开放的功能', () => {
  it('协作已开放 —— 挡它的两个理由都不成立了', async () => {
    // 当初挡住协作的理由是「桌面端没界面、agent 还不能在群里发言」。阶段 27 把
    // 这两条都做掉了，P0 验收也跑完了（2026-09-07），所以闸门开了。
    //
    // 这条用例留着不是为了断言集合是空的，是为了**让下一次收起协作入口必须先
    // 改这里** —— 顺手加回 UNRELEASED 而不解释理由，会在这里停下。
    const { UNRELEASED } = await import('@/composables/useMenus')

    expect(UNRELEASED.has('collab')).toBe(false)
  })

  it('未开放名单里的名字必须是真路由', async () => {
    // 拼错一个名字，这份名单就静默失效 —— 而表现是「说好不显示的东西显示出来了」，
    // 没有任何报错。这条与 MENU_ORDER 那条是同一类守卫。
    const { UNRELEASED } = await import('@/composables/useMenus')
    const { routes } = await import('@/router/routes')

    const allNames = new Set<string>()
    const walk = (list: readonly any[]) => {
      for (const r of list) {
        if (r.name) {
          allNames.add(r.name as string)
        }
        if (r.children) {
          walk(r.children)
        }
      }
    }
    walk(routes as readonly any[])

    expect([...UNRELEASED].filter(n => !allNames.has(n))).toEqual([])
  })

  it('名单是空的时候不会悄悄挡掉任何东西', async () => {
    // 反向基线：把 UNRELEASED 改成挡一切（比如误写成挡所有名字）时，
    // 这条会连同上面的菜单用例一起红。
    const { UNRELEASED, MENU_ORDER } = await import('@/composables/useMenus')

    expect(UNRELEASED.size).toBeLessThan(MENU_ORDER.length)
  })
})
