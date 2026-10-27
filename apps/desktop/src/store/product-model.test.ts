import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setRuntimeI18nLocale } from '@/i18n'

import { $notifications, clearNotifications } from './notifications'
import {
  $productModelAvailability,
  availabilityFromGate,
  availabilityFromSync,
  ensureModelBeforeSend,
  refreshProductModelAvailability,
  resetProductModelAvailability,
  setProductModelAvailability,
  setSessionModelReconciler
} from './product-model'

const ensure = vi.fn()

beforeEach(() => {
  // 断言钉在英文原文上：en 是文案的源语言，其他语言都从它派生。
  // 让测试跟着当前显示语言走的话，翻译一改测试就红，测的却不是我们关心的东西
  setRuntimeI18nLocale('en')
  ensure.mockReset()
  // 主进程的正常答复。每次发送都会问它一次，个别用例再覆盖成别的结局
  ensure.mockResolvedValue({ status: 'ready', model: 'deepseek-v4.1-flash' })
  clearNotifications()
  $productModelAvailability.set('unknown')
  setSessionModelReconciler(null)
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { productModel: { ensure } } })
})

const banner = () => $notifications.get().find(item => item.id === 'product-model-availability')

describe('availabilityFromSync', () => {
  it('把「没开通」和「没问出来」分成两种状态', () => {
    // 这两条合并成一个布尔的话，用户拿着「联系管理员开通」去找管理员，
    // 对方一看权限明明有，两边都白跑
    expect(availabilityFromSync({ reason: 'no-grants', model: null })).toBe('no-grants')
    expect(availabilityFromSync({ reason: 'unavailable', model: null })).toBe('unavailable')
  })

  it('写配置失败算 unavailable 而不是 ready', () => {
    // 报 ready 的话消息照样发出去，再撞一次 HTTP 400，
    // 等于把一次明确的失败变成一次含糊的失败
    expect(availabilityFromSync({ reason: 'apply-failed', model: null })).toBe('unavailable')
  })

  it('选上了 / 保留原选择 / stock 构建都算就绪', () => {
    expect(availabilityFromSync({ reason: 'selected', model: 'deepseek-v4-flash' })).toBe('ready')
    expect(availabilityFromSync({ reason: 'regranted', model: 'deepseek-v4-flash' })).toBe('ready')
    expect(availabilityFromSync({ reason: 'kept', model: 'deepseek-v4-flash' })).toBe('ready')
    expect(availabilityFromSync({ reason: 'skipped', model: null })).toBe('ready')
    expect(availabilityFromSync(undefined)).toBe('ready')
  })
})

describe('availabilityFromGate', () => {
  it('闸门的三种结局原样映射', () => {
    expect(availabilityFromGate({ status: 'ready', model: 'glm-5' })).toBe('ready')
    expect(availabilityFromGate({ status: 'no-grants' })).toBe('no-grants')
    expect(availabilityFromGate({ status: 'unavailable' })).toBe('unavailable')
  })
})

describe('横幅', () => {
  it('没开通和连不上给的是两句不同的话', () => {
    setProductModelAvailability('no-grants')
    const noGrants = banner()?.message

    setProductModelAvailability('unavailable')
    const unavailable = banner()?.message

    expect(noGrants).toBeTruthy()
    expect(unavailable).toBeTruthy()
    expect(noGrants).not.toBe(unavailable)
    // 网络问题不能说成「联系管理员开通」
    expect(unavailable).not.toMatch(/administrator/i)
  })

  it('横幅不会自动消失，也不会叠成一摞', () => {
    setProductModelAvailability('no-grants')
    setProductModelAvailability('unavailable')

    expect($notifications.get().filter(item => item.id === 'product-model-availability')).toHaveLength(1)
    expect(banner()?.kind).toBe('warning')
  })

  it('恢复正常时主动收掉横幅', () => {
    // 管理员刚开通完，用户手里还挂着「请联系管理员开通」的话，
    // 他会以为没生效又去催一遍
    setProductModelAvailability('no-grants')
    expect(banner()).toBeTruthy()

    setProductModelAvailability('ready')
    expect(banner()).toBeUndefined()
  })

  it('退出登录清干净，不把上一个账号的状态带给下一个', () => {
    setProductModelAvailability('no-grants')
    resetProductModelAvailability()

    expect($productModelAvailability.get()).toBe('unknown')
    expect(banner()).toBeUndefined()
  })
})

describe('refreshProductModelAvailability', () => {
  it('IPC 抛异常算 unavailable，不算没开通', () => {
    ensure.mockRejectedValue(new Error('EPIPE'))

    return refreshProductModelAvailability().then(result => {
      expect(result).toBe('unavailable')
      expect($productModelAvailability.get()).toBe('unavailable')
    })
  })

  it('preload 还没跟上时放行，不凭空造一个故障', async () => {
    // 开发期渲染层会比 preload 早热更新一步；真发不出去的话网关自己会报错
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: {} })

    await expect(refreshProductModelAvailability()).resolves.toBe('ready')
  })
})

describe('ensureModelBeforeSend', () => {
  it('已确认有模型也每次问一次主进程：登录后被下架的默认模型只有这样才会被发现', async () => {
    // 原先 ready 时纯本地放行。2026-09-18 所有内测用户的默认模型都已被下架，
    // 新对话全部 403，而这道闸门一次都没问过（主进程侧的网关往返有 60 秒缓存）
    setProductModelAvailability('ready')

    await expect(ensureModelBeforeSend()).resolves.toBe(true)
    expect(ensure).toHaveBeenCalledTimes(1)
  })

  it('刚重启还没问过时当场查一次', async () => {
    ensure.mockResolvedValue({ status: 'ready', model: 'deepseek-v4-flash' })

    await expect(ensureModelBeforeSend()).resolves.toBe(true)
    expect(ensure).toHaveBeenCalledTimes(1)
    expect($productModelAvailability.get()).toBe('ready')
  })

  it('管理员刚开通：重查选上模型并放行，用户不必重新登录', async () => {
    setProductModelAvailability('no-grants')
    ensure.mockResolvedValue({ status: 'ready', model: 'deepseek-v4-flash' })

    await expect(ensureModelBeforeSend()).resolves.toBe(true)
    // 横幅也一并收掉，否则他发出去了却还看着一条「请联系管理员开通」
    expect(banner()).toBeUndefined()
  })

  it('确实没开通：拦住，并保持「联系管理员」这句话', async () => {
    ensure.mockResolvedValue({ status: 'no-grants' })

    await expect(ensureModelBeforeSend()).resolves.toBe(false)
    expect(banner()?.message).toMatch(/administrator/i)
  })

  it('网关连不上：同样拦住，但说的是重试而不是找管理员', async () => {
    ensure.mockResolvedValue({ status: 'unavailable' })

    await expect(ensureModelBeforeSend()).resolves.toBe(false)
    expect(banner()?.message).not.toMatch(/administrator/i)
  })

  it('每次被拦住都会重查一次，而不是记着上次的结论', async () => {
    ensure.mockResolvedValue({ status: 'no-grants' })

    await ensureModelBeforeSend()
    await ensureModelBeforeSend()

    // 用户按发送这个动作本身就是「我觉得现在应该可以了」，
    // 缓存住上次结论的话，管理员开通后他仍然只能靠重新登录
    expect(ensure).toHaveBeenCalledTimes(2)
  })
})

describe('ensureModelBeforeSend · 会话级模型对账', () => {
  it('有模型也要问一句「我这个会话选的那个还在不在」，并把闸门结果一并交过去', async () => {
    setProductModelAvailability('ready')
    const reconcile = vi.fn().mockResolvedValue(true)

    setSessionModelReconciler(reconcile)

    await expect(ensureModelBeforeSend()).resolves.toBe(true)
    expect(reconcile).toHaveBeenCalledTimes(1)
    expect(reconcile).toHaveBeenCalledWith(null, { status: 'ready', model: 'deepseek-v4.1-flash' })
  })

  it('主进程刚换掉被下架的默认模型：replacedFrom 原样交给对账，由它对齐新对话并提示', async () => {
    const gate = { status: 'ready', model: 'deepseek-v4.1-flash', replacedFrom: 'deepseek-v4-flash' }
    const reconcile = vi.fn().mockResolvedValue(true)

    setSessionModelReconciler(reconcile)
    ensure.mockResolvedValue(gate)

    await expect(ensureModelBeforeSend()).resolves.toBe(true)
    expect(reconcile).toHaveBeenCalledWith(null, gate)
  })

  it('对账说发不了就拦住 —— 撤销后既没切成也没得切', async () => {
    setProductModelAvailability('ready')
    setSessionModelReconciler(vi.fn().mockResolvedValue(false))

    await expect(ensureModelBeforeSend()).resolves.toBe(false)
  })

  it('账号本身就没有可用模型时，先拦在前一道闸门，不再多问一次', async () => {
    const reconcile = vi.fn().mockResolvedValue(true)

    setSessionModelReconciler(reconcile)
    ensure.mockResolvedValue({ status: 'no-grants' })

    await expect(ensureModelBeforeSend()).resolves.toBe(false)
    expect(reconcile).not.toHaveBeenCalled()
  })

  it('没登记对账函数就放行 —— 与本文件其余降级策略一致', async () => {
    setProductModelAvailability('ready')

    await expect(ensureModelBeforeSend()).resolves.toBe(true)
  })
})
