import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setRuntimeI18nLocale } from '@/i18n'

import { $notifications, clearNotifications } from './notifications'
import {
  $productGatewayReachable,
  applyGatewayReachability,
  isProductGatewayUnreachable,
  resetProductGatewayReachable,
  setProductGatewayReachable
} from './product-gateway'

beforeEach(() => {
  // 断言钉在英文原文上：en 是文案的源语言，翻译一改测试就红的话，
  // 测的就不是我们关心的东西了
  setRuntimeI18nLocale('en')
  clearNotifications()
  $productGatewayReachable.set('unknown')
})

const banner = () => $notifications.get().find(item => item.id === 'product-gateway-unreachable')

describe('可达性状态', () => {
  it('「还没问过」和「连不上」是两回事', () => {
    // 启动瞬间就报连不上，会在每次冷启动闪一下红 —— 那时候只是还没轮询到
    expect(isProductGatewayUnreachable()).toBe(false)
    expect(banner()).toBeUndefined()
  })

  it('连不上时挂横幅，恢复后自己收掉', () => {
    setProductGatewayReachable('unreachable')
    expect(banner()).toBeTruthy()
    expect(isProductGatewayUnreachable()).toBe(true)

    setProductGatewayReachable('reachable')
    expect(banner()).toBeUndefined()
    expect(isProductGatewayUnreachable()).toBe(false)
  })

  it('横幅不自动消失', () => {
    // 自动飘走的话，用户回到电脑前看到的是一个不动的界面和零条解释
    setProductGatewayReachable('unreachable')

    expect(banner()?.kind).toBe('warning')
  })

  it('反复观测不叠横幅', () => {
    setProductGatewayReachable('unreachable')
    setProductGatewayReachable('unreachable')
    setProductGatewayReachable('unreachable')

    expect($notifications.get().filter(item => item.id === 'product-gateway-unreachable')).toHaveLength(1)
  })

  it('退出登录清干净', () => {
    setProductGatewayReachable('unreachable')
    resetProductGatewayReachable()

    expect($productGatewayReachable.get()).toBe('unknown')
    expect(banner()).toBeUndefined()
  })
})

describe('文案', () => {
  it('15 秒探测超时说网关响应慢，不让用户误查网络', () => {
    applyGatewayReachability(false, { failureKind: 'timeout' })

    expect(banner()?.message).toMatch(/slow|respond/i)
    expect(banner()?.message).not.toMatch(/network/i)
  })

  it('只有连接失败才提示检查网络', () => {
    applyGatewayReachability(false, { failureKind: 'connection' })

    expect(banner()?.message).toMatch(/network/i)
  })

  it('HTTP 或响应格式异常说服务器繁忙，不冒充网络故障', () => {
    applyGatewayReachability(false, { failureKind: 'response' })

    expect(banner()?.message).toMatch(/server/i)
    expect(banner()?.message).not.toMatch(/network/i)
  })

  it('说的是检查网络，不是账号出了问题', () => {
    // 两者需要相反的指示。说成账号问题，用户会去找管理员，
    // 管理员一看账号好好的，两边都白跑一趟
    setProductGatewayReachable('unreachable')

    expect(banner()?.message).toMatch(/network/i)
    expect(banner()?.message).not.toMatch(/administrator|sign in again/i)
  })

  it('明说会自动恢复，免得用户以为要自己做点什么', () => {
    setProductGatewayReachable('unreachable')

    expect(banner()?.message).toMatch(/retr(y|ies|ying)|recover/i)
  })
})

describe('恢复出口', () => {
  it('横幅带一个能立刻重查的按钮', () => {
    // 60 秒轮询是节奏不是出口 —— 这一格要求的是「有限重试**与恢复入口**」
    const onRetry = vi.fn()

    setProductGatewayReachable('unreachable', { onRetry })
    banner()?.action?.onClick()

    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('没给 onRetry 时也不崩，只是没有按钮', () => {
    setProductGatewayReachable('unreachable')

    expect(banner()).toBeTruthy()
    expect(banner()?.action).toBeUndefined()
  })
})

describe('applyGatewayReachability', () => {
  it('true / false 分别落到两个状态', () => {
    applyGatewayReachability(false)
    expect($productGatewayReachable.get()).toBe('unreachable')

    applyGatewayReachability(true)
    expect($productGatewayReachable.get()).toBe('reachable')
  })

  it('undefined 当成「没问出来」，不读成 false', () => {
    // 老版本 preload 不带这个字段。读成 false 的话，开发期热更新窗口里
    // 会凭空冒出一条「连不上公司网关」
    applyGatewayReachability(undefined)

    expect($productGatewayReachable.get()).toBe('unknown')
    expect(banner()).toBeUndefined()
  })

  it('把重试出口透传下去', () => {
    const onRetry = vi.fn()

    applyGatewayReachability(false, { onRetry })
    banner()?.action?.onClick()

    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})
