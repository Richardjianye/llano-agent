import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus } from '@/global'
import { $mandatoryUpdate, resetMandatoryUpdate } from '@/store/product-mandatory-update'
import { $updateApply, resetUpdateApplyState } from '@/store/updates'

import { ProductMandatoryUpdateOverlay } from './product-mandatory-update-overlay'

const INSTALL = /install now|立即安装/i
const RETRY = /try again|重试/i
const QUIT = /^(quit|退出)$/i
const INSTALLING = /installing|正在安装/i

const blocking = (extra: Partial<DesktopUpdateStatus> = {}): DesktopUpdateStatus => ({
  currentVersion: '1.2.0',
  fetchedAt: Date.now(),
  mandatory: true,
  supported: true,
  targetVersion: '1.3.0',
  updateAvailable: true,
  updatePolicy: 'required',
  ...extra
})

let restoreDesktop: (() => void) | null = null

function stubDesktop(updates: Record<string, unknown>) {
  const original = window.hermesDesktop
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { updates } })
  restoreDesktop = () => Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
}

afterEach(() => {
  cleanup()
  restoreDesktop?.()
  restoreDesktop = null
  resetMandatoryUpdate()
  resetUpdateApplyState()
})

describe('ProductMandatoryUpdateOverlay', () => {
  it('不强制时完全不出现', () => {
    render(<ProductMandatoryUpdateOverlay />)

    expect(screen.queryByTestId('mandatory-update-overlay')).toBeNull()
  })

  it('强制时全屏阻断，并说明当前版本与目标版本', () => {
    $mandatoryUpdate.set(blocking())
    render(<ProductMandatoryUpdateOverlay />)

    const overlay = screen.getByTestId('mandatory-update-overlay')

    expect(overlay.className).toContain('fixed inset-0')
    expect(overlay.className).toContain('z-(--z-forced-update)')
    expect(overlay.textContent).toContain('1.2.0')
    expect(overlay.textContent).toContain('1.3.0')
  })

  it('只有安装和退出两个出口，没有关闭、没有稍后再说', () => {
    $mandatoryUpdate.set(blocking())
    render(<ProductMandatoryUpdateOverlay />)

    const labels = screen.getAllByRole('button').map(node => node.textContent?.trim() ?? '')

    expect(labels).toHaveLength(2)
    expect(labels.some(label => INSTALL.test(label))).toBe(true)
    expect(labels.some(label => QUIT.test(label))).toBe(true)
    // snooze / 稍后 / 关闭 一个都不能有 —— 强制更新一旦能推迟就不是强制
    expect(labels.some(label => /later|not now|dismiss|close|稍后|以后|关闭/i.test(label))).toBe(false)
  })

  it('按 ESC 关不掉', () => {
    $mandatoryUpdate.set(blocking())
    render(<ProductMandatoryUpdateOverlay />)

    fireEvent.keyDown(screen.getByTestId('mandatory-update-overlay'), { code: 'Escape', key: 'Escape' })
    fireEvent.keyDown(document, { code: 'Escape', key: 'Escape' })

    expect(screen.queryByTestId('mandatory-update-overlay')).not.toBeNull()
  })

  it('点遮罩背景关不掉', () => {
    $mandatoryUpdate.set(blocking())
    render(<ProductMandatoryUpdateOverlay />)

    fireEvent.click(screen.getByTestId('mandatory-update-overlay'))

    expect(screen.queryByTestId('mandatory-update-overlay')).not.toBeNull()
  })

  it('焦点跑到遮罩外面会被拽回来（否则底下的输入框仍然能用）', async () => {
    $mandatoryUpdate.set(blocking())

    const outside = document.createElement('input')
    document.body.append(outside)

    try {
      render(<ProductMandatoryUpdateOverlay />)

      const overlay = screen.getByTestId('mandatory-update-overlay')
      outside.focus()

      await waitFor(() => expect(overlay.contains(document.activeElement)).toBe(true))
    } finally {
      outside.remove()
    }
  })

  it('安装按钮走公司更新通道', async () => {
    const apply = vi.fn(async () => ({ handedOff: true, ok: true }))
    stubDesktop({ apply })
    $mandatoryUpdate.set(blocking())
    render(<ProductMandatoryUpdateOverlay />)

    fireEvent.click(screen.getByRole('button', { name: INSTALL }))

    await waitFor(() => expect(apply).toHaveBeenCalledTimes(1))
  })

  it('安装中禁掉安装按钮，但退出仍然可用', () => {
    $mandatoryUpdate.set(blocking())
    $updateApply.set({ ...$updateApply.get(), applying: true, message: '正在下载…', stage: 'fetch' })
    render(<ProductMandatoryUpdateOverlay />)

    expect(screen.getByRole('button', { name: INSTALLING })).toHaveProperty('disabled', true)
    expect(screen.getByRole('button', { name: QUIT })).toHaveProperty('disabled', false)
  })

  it('失败后给的是重试，并且说清楚旧版本没被动过', () => {
    const apply = vi.fn(async () => ({ ok: false }))
    stubDesktop({ apply })
    $mandatoryUpdate.set(blocking())
    $updateApply.set({
      ...$updateApply.get(),
      applying: false,
      error: 'artifact-digest-mismatch',
      message: '安装包校验不通过',
      stage: 'error'
    })
    render(<ProductMandatoryUpdateOverlay />)

    expect(screen.getByText('安装包校验不通过')).toBeTruthy()
    expect(screen.getByTestId('mandatory-update-overlay').textContent).toMatch(/not touched|没有被改动/)
    fireEvent.click(screen.getByRole('button', { name: RETRY }))
    expect(apply).toHaveBeenCalledTimes(1)
  })

  it('退出按钮调用主进程退出', () => {
    const quitApp = vi.fn(async () => undefined)
    stubDesktop({ quitApp })
    $mandatoryUpdate.set(blocking())
    render(<ProductMandatoryUpdateOverlay />)

    fireEvent.click(screen.getByRole('button', { name: QUIT }))

    expect(quitApp).toHaveBeenCalledTimes(1)
  })

  it('发行说明原样显示', () => {
    $mandatoryUpdate.set(blocking({ releaseNotes: '修复了脱敏在长文本上的漏字' }))
    render(<ProductMandatoryUpdateOverlay />)

    expect(screen.getByText('修复了脱敏在长文本上的漏字')).toBeTruthy()
  })
})

// CUSTOM-BEGIN: 后台已下好（静默下载 + 下次启动安装）
describe('后台已经下好这一版时', () => {
  const staged = () => blocking({ pendingVersion: '1.3.0' })

  it('不再说「必须立即安装」，而是告诉用户下次启动会自动装好', () => {
    $mandatoryUpdate.set(staged())
    render(<ProductMandatoryUpdateOverlay />)

    expect(screen.getByText(/已下载完成|is downloaded/i)).toBeTruthy()
    expect(screen.getByText(/下次启动|next time you start/i)).toBeTruthy()
  })

  it('主按钮变成「立即重启并安装」，走 installStaged 而不是重新下载', async () => {
    const installStaged = vi.fn().mockResolvedValue({ handedOff: true, ok: true })
    const apply = vi.fn()

    stubDesktop({ apply, installStaged })
    $mandatoryUpdate.set(staged())
    render(<ProductMandatoryUpdateOverlay />)

    fireEvent.click(screen.getByRole('button', { name: /立即重启并安装|restart and install now/i }))

    await waitFor(() => expect(installStaged).toHaveBeenCalledTimes(1))
    // 关键：没有再走一遍下载
    expect(apply).not.toHaveBeenCalled()
  })

  it('待装的版本不是当前要装的那一版时，仍然走正常的下载安装', () => {
    // 后台下的是 1.2.9，网关现在要求 1.3.0 —— 那份暂存包不能算数
    $mandatoryUpdate.set(blocking({ pendingVersion: '1.2.9' }))
    render(<ProductMandatoryUpdateOverlay />)

    expect(screen.getByRole('button', { name: INSTALL })).toBeTruthy()
  })

  it('立即安装失败后回到「重试」，不会卡在已下好的措辞上', async () => {
    const installStaged = vi.fn().mockResolvedValue({ message: '安装器起不来', ok: false })

    stubDesktop({ installStaged })
    $mandatoryUpdate.set(staged())
    render(<ProductMandatoryUpdateOverlay />)

    fireEvent.click(screen.getByRole('button', { name: /立即重启并安装|restart and install now/i }))

    await waitFor(() => expect(screen.getByRole('button', { name: RETRY })).toBeTruthy())
    expect(screen.getByText('安装器起不来')).toBeTruthy()
  })
})
// CUSTOM-END: 后台已下好
