import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { APP_ROUTES, appViewForPath, COLLAB_ROUTE, routeSessionId } from './routes'

/**
 * 项目协作是**独立一级页面**，不是另一种私聊（阶段 27-C）。
 *
 * 这里守的是边界本身：项目路由不能被当成会话路由，项目 store 不能碰私聊会话
 * 状态。串线一旦发生，项目上下文就会被会话恢复、模型切换、个人 skill 注入这些
 * 逻辑当成普通私聊处理 —— 而它们没有一处知道「这条上下文来自群里的别人」。
 */

describe('项目协作路由', () => {
  it('是一个独立的一级页面路由', () => {
    expect(APP_ROUTES.some(route => route.path === COLLAB_ROUTE && route.view === 'collab')).toBe(true)
    expect(appViewForPath(COLLAB_ROUTE)).toBe('collab')
  })

  it('不会被当成会话路由 —— 项目消息不进私聊 Recents', () => {
    // 会话路由是 `/<sessionId>`。协作路由要是漏进保留路径表，它就会被解析成
    // 一个 id 叫 "collab" 的会话，然后出现在 Recents 里。
    expect(routeSessionId(COLLAB_ROUTE)).toBeNull()
  })

  it('排在「消息平台」下方', () => {
    const paths = APP_ROUTES.map(route => route.path)

    expect(paths.indexOf(COLLAB_ROUTE)).toBe(paths.indexOf('/messaging') + 1)
  })
})

describe('项目协作 store 的边界', () => {
  const source = readFileSync(join(__dirname, '..', 'store', 'collab.ts'), 'utf8')

  it('不引用私聊会话状态', () => {
    // 复用 session store 的代价不是重复代码，是串线。真正的防线是「根本没有
    // 那条 import」，所以这里直接对源码断言 —— 行为测试证明不了「没用到」。
    expect(source).not.toMatch(/from '@\/store\/session/)
    expect(source).not.toMatch(/from '\.\/session/)
  })

  it('只经 sidecar 取数，不直接打网关', () => {
    // 渲染进程拿不到 LLANO_GATEWAY_API_KEY，任何直连都只能是错的。
    expect(source).not.toMatch(/https?:\/\//)
    expect(source).toMatch(/from '@\/hermes'/)
  })
})
