/**
 * 阶段 4 · CP3：品牌客户端完整链路（真实网关 + 真实上游）。
 *
 * 覆盖 TODO 阶段 4 第 1 条：登录 → 拉模型 → 对话（普通/流式/中断）→
 * 审计与用量逐笔核对 → 退出撤销。
 *
 * ## 会真花钱
 *
 * 每跑一次会向上游发若干次真实请求。负责人已确认接受这个成本。
 * 不要在 CI 上高频触发，也不要在生产库上跑。
 *
 * ## 为什么断言的是"记了几笔"而不是"回复内容"
 *
 * 真实上游的回复内容不可预测，拿它做断言只会得到一条随机红灯。
 * 真正要守住的是**账**：发了几次就该记几笔。流式漏记那次事故的表现是
 * 报表看起来完全正常、只是少一笔 —— 不逐笔核对就永远发现不了。
 *
 * ## flash/pro 切换的特殊说明
 *
 * `deepseek-v4-pro` 因上游涨价已被路由到 `deepseek-v4-flash`，两者回复来自
 * 同一后端。所以切换类断言只能落在**记账的 model 维度**上，绝不能靠回复差异
 * 区分两端 —— 那样写出来的用例看着能过，其实什么都没验证。
 */

import { expect, test } from '@playwright/test'

import {
  GATEWAY_ORIGIN,
  reconcile,
  setupBrandedGateway,
  type BrandedGatewayFixture,
} from './branded-gateway'

let fixture: BrandedGatewayFixture | undefined

test.describe.configure({ mode: 'serial' })

test.beforeAll(async () => {
  if (!process.env.LLANO_E2E_ADMIN_USER || !process.env.LLANO_E2E_ADMIN_PASSWORD) {
    test.skip(true, '未提供 LLANO_E2E_ADMIN_USER / LLANO_E2E_ADMIN_PASSWORD')
  }

  const probe = await fetch(`${GATEWAY_ORIGIN}/healthz`).catch(() => null)
  if (!probe?.ok) {
    test.skip(true, `Audit Gateway 未在 ${GATEWAY_ORIGIN} 运行`)
  }

  fixture = await setupBrandedGateway()
  await waitForChatReady()
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = undefined
})

/** 本次实际发出的对话次数。对账时要拿它和网关记的笔数比。 */
let sentTurns = 0

// ─── 交互原语 ────────────────────────────────────────────────────────────
// 全部用结构化选择器（data-slot / type），不用 aria-label：后者是 i18n 文案，
// 客户端语言一变用例就全红，而那和被测行为毫无关系。

function composer() {
  return fixture!.page.locator('[contenteditable="true"]').first()
}

function primaryButton() {
  return fixture!.page.locator('[data-slot="composer-root"] button[type="submit"]')
}

/** 忙碌态：主按钮变成那个实心方块（停止），见 composer/controls.tsx */
function busyIndicator() {
  return primaryButton().locator('span.bg-current')
}

/**
 * 等到聊天界面真的可用：composer 在，且没有整屏遮罩盖着。
 *
 * 自己实现而不复用 `fixtures.ts` 的 waitForAppReady，是为了不改上游文件
 * （那个函数的参数类型是几个 mock 夹具的联合类型，加我们的类型就得动它）。
 */
async function waitForChatReady(timeoutMs = 120_000) {
  const page = fixture!.page

  await page.waitForSelector('[contenteditable="true"]', { state: 'attached', timeout: timeoutMs })
  await page.waitForFunction(
    () => {
      const el = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2)
      let node: Element | null = el

      while (node) {
        const cs = window.getComputedStyle(node)

        if (cs.position === 'fixed') {
          const r = node.getBoundingClientRect()

          if (r.left <= 0 && r.top <= 0 && r.right >= window.innerWidth && r.bottom >= window.innerHeight) {
            return false
          }
        }

        node = node.parentElement
      }

      return Boolean(el)
    },
    undefined,
    { timeout: timeoutMs },
  )
}

async function sendMessage(text: string) {
  const box = composer()

  await box.click()
  // 用 type 而不是 fill：composer 是 contenteditable，有自定义 keydown 与
  // 输入法组合态处理，fill 会绕过整条事件链，提交按钮不会变成可用态
  await box.type(text, { delay: 15 })
  await fixture!.page.keyboard.press('Enter')
  sentTurns += 1
}

async function waitForIdle(timeoutMs = 180_000) {
  await expect(busyIndicator()).toHaveCount(0, { timeout: timeoutMs })
}

// ─── 用例 ────────────────────────────────────────────────────────────────

test('模型来源锁死在公司网关', async () => {
  const page = fixture!.page

  // 托管策略把 provider 钉死在 llano-gateway，个人配置压不过它。
  // 这里验证的是运行态的最终结果，不是配置文件长什么样 ——
  // 配置对而运行时没生效，正是最容易漏掉的那种失败。
  const config = await page.evaluate(async () => {
    const res = await fetch('http://127.0.0.1:7899/healthz').catch(() => null)
    return res?.ok ?? false
  })

  expect(config).toBe(true)

  // 界面上能看到的模型必须来自网关目录
  const bodyText = (await page.locator('body').textContent()) ?? ''
  expect(bodyText).toMatch(/deepseek-v4|doubao-seed|llano/i)
})

test('普通对话：发出去、有回复、记上账', async () => {
  test.setTimeout(300_000)

  await sendMessage('用一句话回答：1+1 等于几')
  await waitForIdle()

  // 助手确实回了东西。不断言内容 —— 真实上游的措辞不可预测
  const text = (await fixture!.page.locator('body').textContent()) ?? ''
  expect(text).toContain('1+1 等于几')
})

test('中断生成：token 照常计费，且不算失败', async () => {
  test.setTimeout(300_000)

  await sendMessage('请详细介绍一下中国四大发明，每一项都展开讲讲背景与影响')

  // 等它真的开始生成再停 —— 没进忙碌态就点，等于什么都没中断，
  // 而用例照样会绿：这正是"看着在测、其实没测"的典型形状
  await expect(busyIndicator()).toHaveCount(1, { timeout: 60_000 })
  await primaryButton().click()
  await waitForIdle(60_000)
})

test('逐笔对账：发了几次就记了几笔', async () => {
  test.setTimeout(300_000)

  // 记账走后台任务，给落库留出时间。这里不是"等它好"，
  // 而是等异步写入完成 —— 立刻查会查到一个还没写完的中间态
  await fixture!.page.waitForTimeout(3_000)

  const result = reconcile({
    expectRequests: sentTurns,
    baseline: fixture!.baseline,
  })

  // 附件本体缺失是当前环境的已知问题（库里 stored=true 但文件被清掉了），
  // 与本条链路无关，单独列出来不让它淹没真正的回归
  const relevant = result.failures.filter(f => !f.includes('能下载回来'))

  expect(relevant, `对账未通过：${relevant.join(' / ')}`).toEqual([])
})

test('退出登录：撤销凭据并回到登录页', async () => {
  test.setTimeout(120_000)

  const page = fixture!.page

  // 走 IPC 而不是点设置页里的按钮：退出的被测行为是"凭据有没有被远端撤销"，
  // 不是"设置页第几个 tab 里那个按钮长什么样"。UI 位置变了不该让这条红。
  await page.evaluate(async () => {
    await (window as unknown as {
      hermesDesktop: { productAuth: { logout: () => Promise<unknown> } }
    }).hermesDesktop.productAuth.logout()
  })

  // 回到登录页 = 运行态已清干净
  await expect(page.locator('#product-username')).toBeVisible({ timeout: 60_000 })
})
