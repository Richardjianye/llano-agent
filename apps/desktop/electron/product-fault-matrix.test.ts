/**
 * 阶段 4 · CP5 故障矩阵 —— 主进程侧（"重试有限"这一半）
 *
 * 四格故障 × 两条断言。这个文件只管**重试上限**，"最终 UI 状态"那一半在
 * `src/app/product-fault-matrix.test.tsx` —— 两边的 tsconfig 互不可见
 * （根 `tsconfig.json` 的 `exclude` 里就有 `electron`），所以矩阵物理上只能拆两半，
 * 两个文件的头注释各自列出完整四格，别只读一半就下结论。
 *
 * | 故障 | 重试上限（本文件） | 最终 UI（渲染层文件） |
 * |---|---|---|
 * | Audit Gateway 离线 | 每次请求 15s 超时；重连退避有天花板并在 45s 后升级 | BootFailureOverlay + 模型横幅 `unavailable` |
 * | Token 过期/吊销 | 最多刷新一次、重试一次；账号停用零重试 | 回登录页，文案是"登录凭证已失效" |
 * | Hermes sidecar 异常退出 | 本地失败**闩死**，不再反复重启坏装 | BootFailureOverlay 的 Retry / Repair |
 * | 更新清单无效 | 解析即抛，不重试；应用侧恒 fail-closed | 更新状态 `check-failed`，不阻断业务 |
 *
 * ## 共同底线
 *
 * **不许出现无限重试 / 无限 401 循环。** 每一格都必须有一个能数出来的上限，
 * 以及一个用户点得到的出口。没有出口的"有限重试"只是把死循环换成死界面。
 *
 * ## 关于完备性元测试
 *
 * 绕过矩阵（`tests/hermes_cli/test_product_capability_matrix.py`）有两条元测试，
 * 因为那边有 `product-environments.json` 这个**产品真的会消费**的能力声明可以比对。
 * 故障没有对应的注册表，硬造一个只有测试读的清单是自欺 —— 它永远和现实一致，
 * 因为两边都是同一个人同时编的。所以这里不写元测试，代价是**新增故障类型时要靠
 * 人记得补格**，接力时请照上表逐行核对。
 */

import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import { shouldLatchBackendStartFailure } from './backend-start-failure'
import { type ProductSession, ProductSessionCoordinator } from './product-auth'
import { applyProductUpdate, checkProductUpdate, rememberVerifiedManifest } from './product-updates'
import { normalizeManifest } from './update-manifest'

const GATEWAY = 'http://127.0.0.1:7899'

const session: ProductSession = {
  accessToken: 'jwt',
  apiKey: 'device-key',
  apiKeyId: 7,
  refreshToken: 'refresh',
  username: '员工'
}

/** 一个已经过期的 access token，用来强制走刷新分支。 */
const expiredAccessToken = () => `x.${Buffer.from(JSON.stringify({ exp: 1 })).toString('base64url')}.y`

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body })

const unauthorized = (body: unknown = { message: 'token 已失效' }) => ({
  ok: false,
  status: 401,
  json: async () => body
})

describe('故障 1 · Audit Gateway 离线', () => {
  it('每个网关请求都带 15 秒超时，不会永远挂着', async () => {
    // 没有超时的话，一个不回包（而不是拒连）的网关会让请求悬着，
    // 上层既不成功也不失败 —— 那是比重试风暴更难查的一种"卡死"
    const fetchImpl = vi.fn().mockResolvedValue(ok({ code: 0, data: { uid: 'u1' } }))
    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: GATEWAY })

    await coordinator.validate(session).catch(() => undefined)

    for (const [, init] of fetchImpl.mock.calls) {
      expect(init?.signal, `${fetchImpl.mock.calls[0]?.[0]} 少了超时`).toBeInstanceOf(AbortSignal)
    }
  })

  it('网关连不上时失败一次就抛出，主进程内不自己重试', async () => {
    // 主进程重试 + 渲染层重连循环 = 两层退避相乘，节奏没人算得清。
    // 重试归渲染层那一个循环管，它有 45 秒升级上限（见渲染层矩阵）
    const fetchImpl = vi.fn().mockRejectedValue(new Error('ECONNREFUSED'))
    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: GATEWAY })

    await expect(coordinator.validate(session)).rejects.toThrow('ECONNREFUSED')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
})

describe('故障 2 · Token 过期 / 吊销', () => {
  it('过期 access token 只刷新一次，不是每个请求刷一次', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(ok({ code: 0, data: { access_token: 'fresh', refresh_token: 'fresh-refresh' } }))
      .mockResolvedValueOnce(ok({ code: 0, data: { uid: 'u1' } }))
      .mockResolvedValueOnce(ok({ object: 'list', data: [] }))

    const coordinator = new ProductSessionCoordinator({
      fetchImpl,
      gatewayOrigin: GATEWAY,
      nowSeconds: () => 10
    })

    await coordinator.validate({ ...session, accessToken: expiredAccessToken() })

    const refreshes = fetchImpl.mock.calls.filter(([url]) => String(url).endsWith('/admin/user/refresh'))

    expect(refreshes).toHaveLength(1)
  })

  // 刷新令牌是一次性的：网关收下旧的那一刻就把它作废了。新的一对如果要等
  // info 与 /v1/models 都通过才交出来，休眠唤醒时后两步一超时，唯一有效的
  // 刷新令牌就随异常丢掉，下一轮拿作废的旧令牌去刷新 → 10003 → 被踢回登录页。
  it.each([
    ['access token 已到期，先刷新再查 info', expiredAccessToken(), [ok({ code: 0, data: { access_token: 'fresh', refresh_token: 'fresh-refresh' } })]],
    [
      'info 回 401 后刷新，再查 info',
      'jwt',
      [unauthorized(), ok({ code: 0, data: { access_token: 'fresh', refresh_token: 'fresh-refresh' } })]
    ]
  ])('%s：刷新成功后下一步超时，新令牌已经交出去落盘', async (_label, accessToken, responses) => {
    const fetchImpl = vi.fn()

    for (const response of responses) {
      fetchImpl.mockResolvedValueOnce(response)
    }

    fetchImpl.mockRejectedValueOnce(Object.assign(new Error('The operation was aborted due to timeout'), { name: 'TimeoutError' }))

    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: GATEWAY, nowSeconds: () => 10 })
    const onRotated = vi.fn()

    await expect(coordinator.validate({ ...session, accessToken }, { onRotated })).rejects.toThrow(/timeout/)

    expect(onRotated).toHaveBeenCalledTimes(1)
    expect(onRotated.mock.calls[0][0]).toMatchObject({ accessToken: 'fresh', refreshToken: 'fresh-refresh' })
  })

  it('没有刷新就不回调：不做无谓的落盘', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(ok({ code: 0, data: { uid: 'u1' } }))
      .mockResolvedValueOnce(ok({ object: 'list', data: [] }))

    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: GATEWAY, nowSeconds: () => 10 })
    const onRotated = vi.fn()

    await coordinator.validate(session, { onRotated })

    expect(onRotated).not.toHaveBeenCalled()
  })

  it('刷新后仍然 401：最多再刷一次、再试一次，然后放弃', async () => {
    // 这是"无限 401 循环"最容易长出来的地方：401 → 刷新 → 401 → 刷新 …
    // 上限必须能数出来。刷新 + info + 刷新 + info = 4 次，到此为止
    const fetchImpl = vi.fn().mockImplementation(async (url: string) =>
      String(url).endsWith('/admin/user/refresh')
        ? ok({ code: 0, data: { access_token: 'fresh', refresh_token: 'fresh-refresh' } })
        : unauthorized()
    )

    const coordinator = new ProductSessionCoordinator({
      fetchImpl,
      gatewayOrigin: GATEWAY,
      nowSeconds: () => 10
    })

    await expect(coordinator.validate({ ...session, accessToken: expiredAccessToken() })).rejects.toThrow()

    expect(fetchImpl.mock.calls.map(([url]) => String(url).replace(GATEWAY, ''))).toEqual([
      '/admin/user/refresh',
      '/admin/user/info',
      '/admin/user/refresh',
      '/admin/user/info'
    ])
  })

  it('账号被停用（10007）零重试 —— 再刷多少次也不会变回启用', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(ok({ code: 10007, message: '账户被禁用' }))
    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: GATEWAY })

    await expect(coordinator.validate(session)).rejects.toMatchObject({ code: 10007 })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('设备 Key 被后台吊销：认证通过也照样拒绝，且不重试', async () => {
    // access token 还没到期、账号也没停用，唯独这台机器的 Key 被管理员吊了。
    // 这一格漏掉的话，被强制下线的机器会一直"看起来还登着"
    const fetchImpl = vi.fn().mockImplementation(async (url: string) =>
      String(url).endsWith('/v1/models')
        ? unauthorized({ error: { message: 'API Key 已吊销' } })
        : ok({ code: 0, data: { uid: 'u1' } })
    )

    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: GATEWAY })

    await expect(coordinator.validate(session)).rejects.toThrow('API Key 已吊销')
    expect(fetchImpl.mock.calls.filter(([url]) => String(url).endsWith('/v1/models'))).toHaveLength(1)
  })

  it('并发校验共用一次刷新 —— refresh token 是一次性的，重放就是自己把自己踢下线', async () => {
    let releaseRefresh: ((value: unknown) => void) | undefined
    const refreshGate = new Promise(resolve => (releaseRefresh = resolve))

    const fetchImpl = vi.fn().mockImplementation(async (url: string) => {
      if (String(url).endsWith('/admin/user/refresh')) {
        await refreshGate

        return ok({ code: 0, data: { access_token: 'fresh', refresh_token: 'fresh-refresh' } })
      }

      return String(url).endsWith('/v1/models') ? ok({ object: 'list', data: [] }) : ok({ code: 0, data: { uid: 'u1' } })
    })

    const coordinator = new ProductSessionCoordinator({
      fetchImpl,
      gatewayOrigin: GATEWAY,
      nowSeconds: () => 10
    })

    const expired = { ...session, accessToken: expiredAccessToken() }
    const both = Promise.all([coordinator.validate(expired), coordinator.validate(expired)])

    releaseRefresh?.(undefined)
    await both

    expect(fetchImpl.mock.calls.filter(([url]) => String(url).endsWith('/admin/user/refresh'))).toHaveLength(1)
  })
})

/**
 * 这一格在主进程侧只剩一个判定：**失败要不要闩死**。sidecar 退出后的实际处置
 * （提示、恢复按钮、是否遮住聊天）全在渲染层，见渲染层矩阵的同名一格。
 * 谓词本身的完整分支表在 `backend-start-failure.test.ts`；这里只钉住矩阵关心的
 * 那条不变量，读矩阵的人不必先去翻另一个文件。
 */
describe('故障 3 · Hermes sidecar 异常退出', () => {
  it('本地后端启动失败会闩死，渲染层的重连循环不能把坏装反复重启', () => {
    // 闩不住的话，一个装坏了的本地 sidecar 会被每一次重连尝试重新拉起，
    // 变成一个进程级的重试风暴 —— 机器发烫、日志刷屏、界面还是不动
    expect(shouldLatchBackendStartFailure({ attemptedRemote: false })).toBe(true)
  })

  it('远端后端失败不闩，否则恢复入口全都点不动了', () => {
    // 远端没有子进程的 exit 事件来解闩，闩上就得整个应用重启才能恢复 ——
    // 重连、重新登录、唤醒重探全都会撞同一个缓存住的旧错误
    expect(shouldLatchBackendStartFailure({ attemptedRemote: true })).toBe(false)
  })
})

describe('故障 4 · 更新清单无效', () => {
  // 阶段 5 · U3 把清单格式换成了「单目标 + 签名」。这一格的意图没变：
  // 一份不合法的清单绝不能被"尽力而为"地部分解析出来。
  //
  // 换格式之后风险其实更集中了：policy 现在来自**已验签**的清单，
  // 所以下面既要证明垃圾清单读不出 policy，也要证明没验签的东西压根进不来。
  const vectorsPath = path.resolve(__dirname, '../../../audit-gateway/release/manifest-vectors.json')
  const vectors = JSON.parse(fs.readFileSync(vectorsPath, 'utf8'))
  const PUBLIC_KEY: string = vectors.test_public_key
  const CASE = vectors.cases[0]

  const manifest = (overrides: Record<string, unknown> = {}) => ({ ...CASE.manifest, ...overrides })

  const okResponse = (body: unknown) =>
    ({ ok: true, status: 200, text: async () => JSON.stringify(body) }) as unknown as Response

  it('合法清单照常解析（先钉住基线，否则下面的否定用例可能是假绿）', () => {
    expect(normalizeManifest(manifest())).toMatchObject({ policy: 'required', version: '1.4.0' })
  })

  it.each([
    ['schema 版本不认识', manifest({ schema: 2 })],
    ['缺版本号', manifest({ version: undefined })],
    ['版本号不是数字形态', manifest({ version: 'latest' })],
    ['policy 是编造的值', manifest({ policy: 'mandatory' })],
    ['平台不认识', manifest({ platform: 'linux' })],
    ['架构不认识', manifest({ arch: 'riscv' })],
    ['少了 sha256', manifest({ artifact_sha256: '' })],
    ['sha256 长度不对', manifest({ artifact_sha256: 'abc' })],
    ['体积不是正整数', manifest({ artifact_size: 0 })],
    ['下载地址不是 http(s)', manifest({ artifact_url: 'file:///etc/passwd' })],
    ['整个不是对象', 'not-json']
  ])('%s：直接抛，不做"尽力而为"的部分解析', (_label, payload) => {
    expect(() => normalizeManifest(payload)).toThrow()
  })

  it('篡改成 required 也没用 —— 校验不过就根本读不到 policy', () => {
    // 这一格真正的风险：`required` 会全屏阻断业务且不可关闭。
    // 一份垃圾清单如果能"部分解析"出 policy，等于给了任何能改清单的人
    // 一个让全公司客户端停摆的开关
    expect(() => normalizeManifest(manifest({ policy: 'required', schema: 99 }))).toThrow()
    expect(() => normalizeManifest(manifest({ policy: 'required', version: '../../etc' }))).toThrow()
  })

  it('把 optional 改成 required 会让验签失败，读不出这个开关', async () => {
    // U3 之后这条比"格式校验"更硬：policy 进了签名，改它签名就对不上
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(okResponse({ update: { manifest: manifest({ policy: 'optional' }), signature: CASE.signature } }))

    const status = await checkProductUpdate({
      arch: 'arm64',
      currentVersion: '1.0.0',
      fetchImpl,
      manifestUrl: 'https://example.invalid/v1/client-update',
      platform: 'darwin',
      publicKey: PUBLIC_KEY
    })

    expect(status.updateAvailable).toBe(false)
    expect(status.error).toBe('bad-signature')
    expect(status.updatePolicy).toBeUndefined()
    expect(status.mandatory).toBeUndefined()
  })

  it('清单接口非 2xx 时不当作有更新，且只请求一次', async () => {
    // U3 之前这里是"抛出"，现在收敛成带 code 的失败状态：
    // 对"能不能更新"的答案相同（不能），但调用点不再需要各自 try
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 503, text: async () => '' })

    const status = await checkProductUpdate({
      currentVersion: '1.0.0',
      fetchImpl,
      manifestUrl: 'https://example.invalid/v1/client-update',
      publicKey: PUBLIC_KEY
    })

    expect(status.updateAvailable).toBe(false)
    expect(status.error).toBe('gateway-error')
    expect(status.message).toContain('503')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('清单请求也带超时', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(okResponse({ update: null }))

    await checkProductUpdate({
      currentVersion: '1.0.0',
      fetchImpl,
      manifestUrl: 'https://example.invalid/v1/client-update',
      publicKey: PUBLIC_KEY
    })

    expect(fetchImpl.mock.calls[0]?.[1]?.signal).toBeInstanceOf(AbortSignal)
  })

  it('没有已验签的清单时拒绝安装，且不下载任何东西', async () => {
    // 取代了 U3 之前那条"apply 恒 fail-closed"。现在 apply 是真的会装东西的，
    // 所以要钉住的变成：**只装检查时验过签的那一份**，没有就什么都不做
    rememberVerifiedManifest(null)
    const fetchImpl = vi.fn()

    const result = await applyProductUpdate({
      currentVersion: '1.0.0',
      fetchImpl,
      quitImpl: () => {
        throw new Error('不该退出')
      },
      stagingDir: '/tmp/llano-should-not-be-used'
    })

    expect(result).toMatchObject({ code: 'no-verified-update', handedOff: false, ok: false })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})
