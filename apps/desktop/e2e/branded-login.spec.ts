/**
 * 阶段 4 · CP2 冒烟：品牌客户端能否连真实 Audit Gateway 走通登录。
 *
 * 这条 spec 只证一件事 —— 夹具本身是可用的：开专用账号、起品牌构建、
 * 真登录、拿到网关下发的模型、收尾停用账号。完整链路（切模型 / 流式 / 中断 /
 * 多模态 / 逐笔对账 / 退出撤销）在 CP3 的 spec 里。
 *
 * 需要：Audit Gateway 在跑 + LLANO_E2E_ADMIN_USER/PASSWORD。
 * 缺任一项时**跳过而不是失败** —— 这条依赖外部服务，在没起网关的机器上
 * 报红会让人以为代码坏了，而实际上只是没开网关。
 */

import { expect, test } from '@playwright/test'

import { GATEWAY_ORIGIN, setupBrandedGateway, type BrandedGatewayFixture } from './branded-gateway'

let fixture: BrandedGatewayFixture | undefined

test.beforeAll(async () => {
  if (!process.env.LLANO_E2E_ADMIN_USER || !process.env.LLANO_E2E_ADMIN_PASSWORD) {
    test.skip(true, '未提供 LLANO_E2E_ADMIN_USER / LLANO_E2E_ADMIN_PASSWORD')
  }

  const probe = await fetch(`${GATEWAY_ORIGIN}/healthz`).catch(() => null)
  if (!probe?.ok) {
    test.skip(true, `Audit Gateway 未在 ${GATEWAY_ORIGIN} 运行`)
  }
})

test.afterAll(async () => {
  await fixture?.cleanup()
})

test('品牌客户端能用专用账号登录真实网关', async () => {
  test.setTimeout(240_000)

  fixture = await setupBrandedGateway()

  // 登录页已消失（setupBrandedGateway 内部已等过），这里再确认一次运行态：
  // 登录框不在 = 没被踢回登录页
  await expect(fixture.page.locator('#product-username')).toHaveCount(0)

  // 账号确实是这次新开的，且是员工角色 —— 用总裁跑就测不出权限范围问题
  expect(fixture.account.username).toMatch(/^回归/)
  expect(fixture.baseline).toBeGreaterThanOrEqual(0)
})
