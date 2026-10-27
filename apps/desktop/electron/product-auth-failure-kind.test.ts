import { describe, expect, it } from 'vitest'

import { isProductAccountDisabledError, isProductGatewayAuthError, ProductGatewayError } from './product-auth'

/**
 * CUSTOM（2026-09-01）：把「管理员下线了你」和「会话过期了」分开。
 *
 * ## 这组是一次线上投诉的化石
 *
 * 124 上装的客户端，休眠唤醒后弹回登录页，提示「你已被管理员强制下线」。
 * 管理员什么都没做 —— 真实原因是网关那边 Redis 抖了一下，把刷新请求判成了
 * 已撤销（10003）。而客户端把 10002/10003/10007 三个码**并成一句话**，
 * 于是一次基础设施抖动被说成了一次人事动作。
 *
 * 代价不是「文案不准」：用户拿着这句话去找管理员，管理员一看账号好好的，
 * 两边都白跑一趟，而真正的原因没有任何人在查。
 */
describe('鉴权失败的归因', () => {
  it('账号被禁用（10007）确实是管理员动作', () => {
    expect(isProductAccountDisabledError(new ProductGatewayError('disabled', { code: 10007 }))).toBe(true)
  })

  it('403 同样是人为动作', () => {
    expect(isProductAccountDisabledError(new ProductGatewayError('forbidden', { status: 403 }))).toBe(true)
  })

  it.each([
    ['access_token 失效', { code: 10002 }],
    ['refresh_token 失效', { code: 10003 }],
    ['401', { status: 401 }]
  ])('%s 不是管理员动作 —— 重新登录就好，别惊动任何人', (_label, options) => {
    const error = new ProductGatewayError('x', options)

    // 仍然是鉴权错误（该登出还是要登出）……
    expect(isProductGatewayAuthError(error)).toBe(true)
    // ……但**不该说成管理员干的**。这一条红了就说明归因又被并回去了。
    expect(isProductAccountDisabledError(error)).toBe(false)
  })

  it('不是网关错误的东西一律不算管理员动作', () => {
    // 网络异常走的是另一条路（classifyProductGatewayFailure），
    // 绝不能从这里漏成「管理员下线」。
    expect(isProductAccountDisabledError(new TypeError('fetch failed'))).toBe(false)
    expect(isProductAccountDisabledError(null)).toBe(false)
  })
})
