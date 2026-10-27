import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus } from '@/global'
import { storedString } from '@/lib/storage'
import { $mandatoryUpdate, resetMandatoryUpdate } from '@/store/product-mandatory-update'
import { $optionalUpdatePrompt, closeOptionalUpdatePrompt } from '@/store/product-optional-update'
import { resetUpdateApplyState } from '@/store/updates'

import { ProductOptionalUpdateDialog } from './product-optional-update-dialog'

const optional = (extra: Partial<DesktopUpdateStatus> = {}): DesktopUpdateStatus => ({
  currentVersion: '0.18.3',
  fetchedAt: Date.now(),
  releaseNotes: '- 修复偶发掉登录',
  supported: true,
  targetVersion: '0.18.4',
  updateAvailable: true,
  updatePolicy: 'optional',
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
  closeOptionalUpdatePrompt()
  resetMandatoryUpdate()
  resetUpdateApplyState()
})

describe('ProductOptionalUpdateDialog', () => {
  it('没有可选更新时不出现', () => {
    render(<ProductOptionalUpdateDialog />)

    expect(screen.queryByTestId('optional-update-dialog')).toBeNull()
  })

  it('有可选更新：带蒙版的模态框，写明版本与更新内容', () => {
    $optionalUpdatePrompt.set(optional())
    render(<ProductOptionalUpdateDialog />)

    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByTestId('optional-update-backdrop')).toBeTruthy()
    expect(screen.getByText(/0\.18\.4/, { selector: 'h2' })).toBeTruthy()
    expect(screen.getByText('- 修复偶发掉登录')).toBeTruthy()
  })

  it('点「稍后提醒」关闭并进入 24 小时冷却', () => {
    $optionalUpdatePrompt.set(optional())
    render(<ProductOptionalUpdateDialog />)

    fireEvent.click(screen.getByRole('button', { name: /remind me later|稍后提醒/i }))

    expect($optionalUpdatePrompt.get()).toBeNull()
    expect(Number(storedString('hermes:update-toast-snooze-until'))).toBeGreaterThan(Date.now())
  })

  it('点「立即安装」在框里开始安装', async () => {
    const apply = vi.fn(async () => ({ ok: true }))
    stubDesktop({ apply, onProgress: vi.fn(() => () => undefined) })
    $optionalUpdatePrompt.set(optional())
    render(<ProductOptionalUpdateDialog />)

    fireEvent.click(screen.getByRole('button', { name: /install now|立即安装/i }))

    await waitFor(() => expect(apply).toHaveBeenCalled())
  })

  it('强制更新在场时让位给全屏阻断', () => {
    $optionalUpdatePrompt.set(optional())
    $mandatoryUpdate.set(optional({ mandatory: true, updatePolicy: 'required' }))
    render(<ProductOptionalUpdateDialog />)

    expect(screen.queryByTestId('optional-update-dialog')).toBeNull()
  })
})
