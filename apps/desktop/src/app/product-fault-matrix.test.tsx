/**
 * 阶段 4 · CP5 故障矩阵 —— 渲染层侧（"最终 UI 状态"这一半）
 *
 * 四格故障 × 两条断言。这个文件只管**用户最后看到什么、能点什么**；
 * "重试上限"那一半在 `electron/product-fault-matrix.test.ts` ——
 * 根 `tsconfig.json` 的 `exclude` 里就有 `electron`，两边互不可见，
 * 矩阵物理上只能拆两半。两个文件的头注释各自列出完整四格。
 *
 * | 故障 | 重试上限（主进程文件） | 最终 UI（本文件） |
 * |---|---|---|
 * | 1a Hermes 网关 WS 断开 | 退避有天花板 | 45s 后升级出 BootFailureOverlay，全屏 CONNECTING 让位 |
 * | 1b Audit Gateway 离线 | 每次请求 15s 超时；主进程内不自己重试 | 横幅说"检查网络"并带「立即重试」；不误报成账号问题 |
 * | 2 Token 过期/吊销 | 最多刷新一次、重试一次；账号停用零重试 | 回登录页，中文"被管理员下线"，压掉英文技术通知 |
 * | 3 Hermes sidecar 异常退出 | 本地失败闩死，不反复重启坏装 | 启动期→恢复面板；启动后→常驻错误提示，不遮聊天 |
 * | 4 更新清单无效 | 解析即抛，不重试；应用侧恒 fail-closed | 状态落 `check-failed`，业务照常，不弹阻断层 |
 *
 * ## 共同底线
 *
 * **不许出现无限重试 / 无限 401 循环。** 每一格都必须有一个能数出来的上限，
 * 以及一个用户点得到的出口。没有出口的"有限重试"只是把死循环换成死界面 ——
 * 全屏 CONNECTING 转圈就是这种死界面，所以每一格都要断言它**让位**了。
 *
 * ## ⚠️ 第 1 格为什么是两格（2026-08-20 人工验收挖出来的）
 *
 * 第一版把它当成一格，18 项测试全绿，**测的却是另一个故障**。
 *
 * 审计网关挂掉时 Hermes sidecar 活得好好的，渲染层和它之间的 WS 一直是 open，
 * 于是重连循环根本不启动 —— `shouldEscalateReconnect` 一次都不会被调用，
 * 45 秒升级、恢复面板、CONNECTING 让位，一条都不会发生。人工验收的现象是：
 * 停掉审计网关等 60 秒，界面纹丝不动，直到发消息才撞一句英文的
 * `redaction policy expired`。
 *
 * 教训是通用的，写在这里给下一个人：**一格全绿不等于那个故障被覆盖了**。
 * 写故障矩阵时先问「这个故障发生时，哪个组件会先察觉到？」——如果答不上来，
 * 多半就会像这次一样，测到隔壁那个能察觉的组件上去。
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { completionErrorMessage } from '@/app/session/hooks/use-message-stream/utils'
import { inlineErrorMessage } from '@/app/session/hooks/use-prompt-actions/utils'
import { BootFailureOverlay } from '@/components/boot-failure-overlay'
import { GatewayConnectingOverlay } from '@/components/gateway-connecting-overlay'
import { setRuntimeI18nLocale } from '@/i18n'
import { reconnectBackoffDelayMs } from '@/lib/reconnect-backoff'
import { RECONNECT_ESCALATE_AFTER_MS, shouldEscalateReconnect } from '@/lib/reconnect-escalation'
import { $desktopBoot, failDesktopBoot } from '@/store/boot'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $desktopOnboarding } from '@/store/onboarding'
import { $productAuthenticated } from '@/store/product-auth'
import {
  $productGatewayReachable,
  applyGatewayReachability,
  resetProductGatewayReachable,
  setProductGatewayReachable
} from '@/store/product-gateway'
import { $productModelAvailability, refreshProductModelAvailability } from '@/store/product-model'
import { $gatewayState } from '@/store/session'
import { $updateStatus, checkUpdates } from '@/store/updates'

import { ProductLoginOverlay } from './product-login-overlay'

const healthyBoot = () =>
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: 'ready',
    phase: 'renderer.ready',
    progress: 100,
    running: false,
    timestamp: Date.now(),
    visible: false
  })

const stubDesktop = (value: Record<string, unknown>) =>
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value })

beforeEach(() => {
  // 断言钉在英文原文上：en 是文案的源语言，别让翻译一改测试就红
  setRuntimeI18nLocale('en')
  clearNotifications()
  $productAuthenticated.set(true)
  $productModelAvailability.set('unknown')
  resetProductGatewayReachable()
  $updateStatus.set(null)
  $gatewayState.set('open')
  $desktopOnboarding.set({
    configured: true,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  })
  healthyBoot()
})

afterEach(() => {
  cleanup()
  Reflect.deleteProperty(window, 'hermesDesktop')
  vi.restoreAllMocks()
})

describe('故障 1a · Hermes 网关 WebSocket 断开', () => {
  it('重连退避有天花板 —— 涨到天上去等于人为制造一个死界面', () => {
    // 上限不是为了省流量，是为了让"还在重连"始终是个可信的说法。
    // 第 30 次退避如果已经涨到几小时，界面上写着重连中其实就是骗人
    const ceilings = Array.from({ length: 40 }, (_, attempt) => reconnectBackoffDelayMs(attempt, { baseDelayMs: 300 }))

    for (const delay of ceilings) {
      expect(delay).toBeLessThan(15_000)
    }
  })

  it('持续失败满 45 秒就升级，把全屏 CONNECTING 换成有按钮的恢复面板', () => {
    const failingSince = 0

    expect(shouldEscalateReconnect({ alreadyEscalated: false, failingSince, now: RECONNECT_ESCALATE_AFTER_MS - 1 })).toBe(
      false
    )
    expect(shouldEscalateReconnect({ alreadyEscalated: false, failingSince, now: RECONNECT_ESCALATE_AFTER_MS })).toBe(
      true
    )
  })

  it('升级是一次性的，不会每个退避 tick 重弹一次错误提示', () => {
    // 每 tick 重弹的话，错误提示会连同它的触感反馈一起堆成一摞，
    // 用户想点恢复按钮都点不到
    expect(
      shouldEscalateReconnect({ alreadyEscalated: true, failingSince: 0, now: RECONNECT_ESCALATE_AFTER_MS * 10 })
    ).toBe(false)
  })

  it('还没失败过就不该升级（健康态别被误判成故障）', () => {
    expect(shouldEscalateReconnect({ alreadyEscalated: false, failingSince: null, now: Date.now() })).toBe(false)
  })

  it('升级之后：恢复面板出现，且带着重试与日志两个出口', async () => {
    stubDesktop({ getRecentLogs: async () => ({ lines: [] }) })
    failDesktopBoot('Could not connect to Hermes gateway')

    render(<BootFailureOverlay />)

    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /open logs/i })).toBeTruthy()
    // 品牌构建锁死托管网关地址，所以这里**不该**有改网关地址的入口
    expect(screen.queryByRole('button', { name: /gateway settings/i })).toBeNull()
  })

  it('恢复面板一出现，全屏 CONNECTING 就必须让位', () => {
    // 两个全屏层同时在的话，用户看到的是转圈那层，恢复按钮全被盖住 ——
    // 这正是「有限重试却仍然是死界面」的样子
    //
    // 注：这条不变量在 gateway-connecting-overlay 里由**两道**守卫共同保证
    // （`connecting` 里的 `!boot.error`，以及后面 `if (boot.error) return null`）。
    // 做变异验证时只拆一道会看到测试仍然绿 —— 那不是测试失效，是冗余生效；
    // 两道一起拆才会红（已实测）
    $gatewayState.set('closed')
    failDesktopBoot('Could not connect to Hermes gateway')

    const { container } = render(<GatewayConnectingOverlay />)

    expect(container.innerHTML).toBe('')
  })

})

describe('故障 1b · Audit Gateway 离线', () => {
  it('连不上时给横幅，而不是让界面一动不动', () => {
    // 这一格原本整个不存在：审计网关挂掉时没有任何组件会察觉，
    // 用户看到的是一个正常的对话界面，直到发消息才报错
    setProductGatewayReachable('unreachable')

    const banner = $notifications.get().find(item => item.id === 'product-gateway-unreachable')

    expect(banner).toBeTruthy()
    expect(banner?.kind).toBe('warning')
    // 持续状态，不能自动飘走
    expect(banner?.message).toMatch(/network|网络/i)
  })

  it('横幅带一个用户点得到的出口', () => {
    // 60 秒轮询是节奏，不是出口。没有出口的有限重试只是把死循环换成死界面
    const onRetry = vi.fn()

    setProductGatewayReachable('unreachable', { onRetry })
    $notifications.get().find(item => item.id === 'product-gateway-unreachable')?.action?.onClick()

    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('说的是检查网络，绝不能说成账号问题', () => {
    // 两者需要相反的指示。说成账号问题，用户会去找管理员，
    // 管理员一看账号好好的，两边都白跑
    setProductGatewayReachable('unreachable')

    const banner = $notifications.get().find(item => item.id === 'product-gateway-unreachable')

    expect(banner?.message).not.toMatch(/administrator|sign in again/i)
  })

  it('恢复连接后横幅自动收掉', () => {
    setProductGatewayReachable('unreachable')
    setProductGatewayReachable('reachable')

    expect($notifications.get().find(item => item.id === 'product-gateway-unreachable')).toBeUndefined()
  })

  it('「还没问过」不算连不上，冷启动不该闪一下红', () => {
    applyGatewayReachability(undefined)

    expect($productGatewayReachable.get()).toBe('unknown')
    expect($notifications.get()).toEqual([])
  })

  it('网关连不上时模型横幅让位，不让用户自己判断哪条是根因', async () => {
    // 「暂时无法确认可用模型」只是网关离线的下游症状。两条横幅一起挂着，
    // 用户得自己判断先修哪个 —— 那不是用户该做的事
    setProductGatewayReachable('unreachable')
    stubDesktop({ productModel: { ensure: vi.fn().mockRejectedValue(new Error('ECONNREFUSED')) } })

    await expect(refreshProductModelAvailability()).resolves.toBe('unavailable')

    // 状态照常写：发送闸门仍然要拦住
    expect($productModelAvailability.get()).toBe('unavailable')
    expect($notifications.get().find(item => item.id === 'product-model-availability')).toBeUndefined()
  })

  it('网关好着但模型没问出来时，模型横幅照常出现', async () => {
    // 让位只在网关确实连不上时发生，别把它变成「模型横幅永远不出现」
    setProductGatewayReachable('reachable')
    stubDesktop({ productModel: { ensure: vi.fn().mockResolvedValue({ status: 'unavailable' }) } })

    await refreshProductModelAvailability()

    const banner = $notifications.get().find(item => item.id === 'product-model-availability')

    expect(banner).toBeTruthy()
    expect(banner?.message).not.toMatch(/administrator/i)
  })

  it('英文的 redaction policy expired 不许出现在聊天气泡里', () => {
    // fail-closed 本身是对的（决策 #9：拿不到脱敏策略就绝不能发），
    // 但抛给业务人员的是开发者措辞的英文 —— 和阶段 4 一开始那个
    // 「HTTP 400: 缺少 model 参数」是同一类问题
    const shown = inlineErrorMessage(new Error('redaction policy expired'), 'fallback')

    expect(shown).not.toMatch(/redaction policy/i)
    expect(shown).toMatch(/network connection|网络连接/i)
  })

  // ⚠️ 上面那条 2026-08-20 就绿着，可 2026-08-21 第二轮人工验收里
  // `redaction policy expired` **照样原样出现在气泡里**。原因是它测的是
  // `inlineErrorMessage`，而那是「提交时就抛错」那条路；真实情况是提交成功、
  // turn 跑了十秒、错误由**终止帧**带回来，落在 `completeAssistantMessage`，
  // 那里从来没跑过措辞表。
  //
  // 所以下面这几条一律打 `completionErrorMessage` —— 用户看到的那个字符串
  // 就是它的返回值。测函数不等于测到那条路。
  it('turn 跑起来之后才失败的，气泡里也不许是英文原文', () => {
    const shown = completionErrorMessage({ error: 'redaction policy expired', partial: false }, '')

    expect(shown).not.toMatch(/redaction policy/i)
    expect(shown).toMatch(/network connection|网络连接/i)
  })

  it('网关停着时重试撞的那句 Connection error. 同样要换成人话', () => {
    // 第一发撞 fail-closed，重试那发连 TCP 都建不起来，
    // OpenAI SDK 抛的是一句光秃秃的 `Connection error.`
    const shown = completionErrorMessage({ error: 'Connection error.', partial: false }, '')

    expect(shown).not.toMatch(/^connection error/i)
    expect(shown).toMatch(/network connection|网络连接/i)
  })

  it('没有规则命中的错误保持原文，不能被换成一句笼统的失败提示', () => {
    // 排查时那串技术细节往往是唯一线索。`summarizeErrorMessage` 的
    // 「超过 180 字就退回 fallback」在这条路上必须不生效
    const raw = `Upstream returned an unexpected shape: ${'x'.repeat(300)}`

    expect(completionErrorMessage({ error: raw, partial: false }, '')).toBe(raw)
  })

  it('这一轮没出错就不该凭空造出一个错误', () => {
    expect(completionErrorMessage(undefined, '桌面上有 12 个文件。')).toBeNull()
  })
})

describe('故障 2 · Token 过期 / 吊销', () => {
  const productAuth = (status: Record<string, unknown>) => ({
    login: vi.fn(),
    logout: vi.fn(),
    onProgress: vi.fn(() => () => undefined),
    onChanged: vi.fn(() => () => undefined),
    status: vi.fn(async () => status)
  })

  it('被强制下线后回登录页，说的是中文业务话术而不是网关原文', async () => {
    const raw = 'API Key revoked (HTTP 401): gateway_auth_failed'

    stubDesktop({ productAuth: productAuth({ authenticated: false, message: raw, reason: 'session-invalid', username: null }) })

    render(<ProductLoginOverlay />)

    await waitFor(() => expect($productAuthenticated.get()).toBe(false))
    expect(screen.queryByText(raw)).toBeNull()
    expect(screen.getByText(/credentials are no longer valid|登录凭证已失效/)).toBeTruthy()
  })

  it('强制下线会压掉抢跑的英文技术通知，不让两套诊断同屏打架', async () => {
    // sidecar 停掉引发的启动失败提示会和认证检查抢跑。认证结论一旦确定，
    // 那条提示就是过期信息，留着只会让用户以为是两个故障
    const { notify } = await import('@/store/notifications')

    notify({ kind: 'error', message: 'Sign in before starting Hermes', title: 'Desktop boot failed' })
    stubDesktop({ productAuth: productAuth({ authenticated: false, reason: 'session-invalid', username: null }) })

    render(<ProductLoginOverlay />)

    await waitFor(() => expect($notifications.get()).toEqual([]))
  })

  it('登录门禁挡人时，后端启动失败的恢复面板不叠在登录页上面', () => {
    // 品牌构建里"账号失效"会主动停掉 sidecar。那是认证结果不是启动事故，
    // 弹恢复面板等于让用户去点 Retry / Repair 修一个根本没坏的东西
    $productAuthenticated.set(false)
    failDesktopBoot('Hermes exited during startup')

    render(<BootFailureOverlay />)

    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('未登录时全屏 CONNECTING 也不出现 —— 登录页才是这时该看到的东西', () => {
    $productAuthenticated.set(false)
    $gatewayState.set('closed')
    $desktopBoot.set({ ...$desktopBoot.get(), progress: 20, running: true, visible: true })

    const { container } = render(<GatewayConnectingOverlay />)

    expect(container.innerHTML).toBe('')
  })
})

describe('故障 3 · Hermes sidecar 异常退出', () => {
  it('启动期退出：恢复面板给的是重试与修复安装，不是让用户自己去找日志', () => {
    stubDesktop({ getRecentLogs: async () => ({ lines: [] }) })
    failDesktopBoot('Hermes exited during startup')

    render(<BootFailureOverlay />)

    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /restart backend|repair/i })).toBeTruthy()
  })

  it('启动完成后退出：只出提示，全屏面板不许盖住已有的聊天记录', async () => {
    // 已经启动成功过的会话里，用户可能正想把刚才那段回复复制走。
    // 盖住聊天去修一个后台进程，是把一次可恢复故障升级成"数据够不着"。
    // 这里模拟 onBackendExit 在启动完成后走的那条分支：只 notify，不 failDesktopBoot
    const { notify } = await import('@/store/notifications')

    healthyBoot()
    notify({ durationMs: 0, kind: 'error', message: 'Hermes stopped unexpectedly', title: 'Backend stopped' })

    render(<BootFailureOverlay />)

    // 提示在，面板不在 —— 一条错误提示本身**不能**把恢复面板拉起来
    expect($notifications.get()).toHaveLength(1)
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('错误提示默认就不自动消失，用户不会回来发现界面不动却没有任何解释', async () => {
    // 钉的是 notify 的默认行为而不是调用点传的参数：调用点写没写 durationMs
    // 是可以改的，"error 不自动消失"这条默认值才是这一格真正依赖的东西
    const { notify } = await import('@/store/notifications')

    vi.useFakeTimers()

    try {
      notify({ kind: 'error', message: 'Hermes stopped unexpectedly', title: 'Backend stopped' })
      vi.advanceTimersByTime(10 * 60 * 1000)

      expect($notifications.get()).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('故障 4 · 更新清单无效', () => {
  it('清单坏掉只落一个 check-failed 状态，不抛异常炸掉调用方', async () => {
    stubDesktop({ updates: { check: vi.fn().mockRejectedValue(new Error('Unsupported update manifest schema.')) } })

    await expect(checkUpdates()).resolves.toMatchObject({ error: 'check-failed' })
    expect($updateStatus.get()?.error).toBe('check-failed')
  })

  it('检查失败不弹"有新版本"，更不弹强制更新的阻断层', async () => {
    // 这一格真正的风险是反向的：一份读不懂的清单如果被当成"有更新"，
    // 而 policy 又恰好被解析成 required，就会全屏阻断全公司的业务
    stubDesktop({ updates: { check: vi.fn().mockRejectedValue(new Error('Update manifest contains an invalid artifact.')) } })

    await checkUpdates()

    expect($updateStatus.get()?.updateAvailable).toBeFalsy()
    expect($updateStatus.get()?.updatePolicy).toBeUndefined()
    expect($notifications.get()).toEqual([])
  })

  it('失败保留上一次的 supported，不把一次网络抖动记成"这机器不支持更新"', async () => {
    $updateStatus.set({ supported: true, updateAvailable: false, fetchedAt: Date.now() })
    stubDesktop({ updates: { check: vi.fn().mockRejectedValue(new Error('boom')) } })

    await checkUpdates()

    expect($updateStatus.get()?.supported).toBe(true)
  })

  it('桥不在时安静返回，不把 stock/开发期构建拖进错误态', async () => {
    stubDesktop({})

    await expect(checkUpdates()).resolves.toBeNull()
    expect($notifications.get()).toEqual([])
  })
})

describe('网关鉴权失败的原文不进聊天气泡', () => {
  it.each(['Error code: 401 - API Key 已吊销', '账号已被禁用', 'HTTP 401: 身份凭证已失效，请重新登录'])('%s', raw => {
    const shown = completionErrorMessage({ error: raw, partial: false }, '')

    expect(shown).toMatch(/credentials are no longer valid|登录凭证已失效/)
    expect(shown).not.toMatch(/API Key|吊销|禁用/)
  })
})
