import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus } from '@/global'
import {
  $mandatoryUpdate,
  ingestUpdateStatusForBlocking,
  resetMandatoryUpdate
} from '@/store/product-mandatory-update'
import { $updateStatus, checkUpdates } from '@/store/updates'

afterEach(() => {
  resetMandatoryUpdate()
  $updateStatus.set(null)
  vi.unstubAllGlobals()
})

const status = (extra: Partial<DesktopUpdateStatus> = {}): DesktopUpdateStatus => ({
  currentVersion: '1.2.0',
  fetchedAt: Date.now(),
  supported: true,
  updateAvailable: true,
  ...extra
})

describe('强制更新闩锁', () => {
  it('已验签清单判定为强制时开始阻断', () => {
    ingestUpdateStatusForBlocking(status({ mandatory: true, targetVersion: '1.3.0' }))

    expect($mandatoryUpdate.get()?.targetVersion).toBe('1.3.0')
  })

  it('可选更新不阻断', () => {
    ingestUpdateStatusForBlocking(status({ mandatory: false, targetVersion: '1.3.0', updatePolicy: 'optional' }))

    expect($mandatoryUpdate.get()).toBeNull()
  })

  it('没有 mandatory 字段时按不强制处理，不去猜 updatePolicy', () => {
    // 判定只有一个来源：主进程从已验签清单算出来的 mandatory。渲染层若在这里
    // 拿 updatePolicy 兜底，就成了第四份判定实现，必然与另外三份漂移。
    ingestUpdateStatusForBlocking(status({ targetVersion: '1.3.0', updatePolicy: 'required' }))

    expect($mandatoryUpdate.get()).toBeNull()
  })

  it('检查失败不解除已经生效的阻断', () => {
    ingestUpdateStatusForBlocking(status({ mandatory: true, targetVersion: '1.3.0' }))

    ingestUpdateStatusForBlocking(status({ error: 'check-failed', message: '网络不通', updateAvailable: false }))

    expect($mandatoryUpdate.get()?.targetVersion).toBe('1.3.0')
  })

  it('检查成功且不再强制时解除阻断（版本追上了，或那一版被下架）', () => {
    ingestUpdateStatusForBlocking(status({ mandatory: true, targetVersion: '1.3.0' }))

    ingestUpdateStatusForBlocking(status({ currentVersion: '1.3.0', updateAvailable: false }))

    expect($mandatoryUpdate.get()).toBeNull()
  })

  it('null 状态不改变阻断', () => {
    ingestUpdateStatusForBlocking(status({ mandatory: true, targetVersion: '1.3.0' }))

    ingestUpdateStatusForBlocking(null)

    expect($mandatoryUpdate.get()?.targetVersion).toBe('1.3.0')
  })
})

describe('checkUpdates 把结果并进阻断状态', () => {
  const stubBridge = (check: () => Promise<DesktopUpdateStatus>) => {
    vi.stubGlobal('window', {
      ...globalThis.window,
      hermesDesktop: { updates: { check } }
    })
  }

  it('一次成功的检查就能让遮罩生效', async () => {
    stubBridge(async () => status({ mandatory: true, targetVersion: '2.0.0' }))

    await checkUpdates()

    expect($mandatoryUpdate.get()?.targetVersion).toBe('2.0.0')
  })

  it('断网不能成为绕过强制更新的办法', async () => {
    // 这条是整个 U4 的要害：轮询每半小时跑一次，只要一次网络抖动能把遮罩清掉，
    // 「强制更新」就等于「拔网线即可跳过」。
    stubBridge(async () => status({ mandatory: true, targetVersion: '2.0.0' }))
    await checkUpdates()

    stubBridge(async () => {
      throw new Error('getaddrinfo ENOTFOUND gateway')
    })
    await checkUpdates()

    expect($updateStatus.get()?.error).toBe('check-failed')
    expect($mandatoryUpdate.get()?.targetVersion).toBe('2.0.0')
  })
})
