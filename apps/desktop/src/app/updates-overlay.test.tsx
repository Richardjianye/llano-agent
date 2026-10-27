/**
 * 可选更新面板里的发行说明（CUSTOM · 阶段 5 · U3 回归）
 *
 * 曾经的现象：运维在网关后台写了一整段「本次更新修了什么」，签进清单下发，
 * 客户端从头到尾没显示过 —— 面板只渲染 `status.commits`（上游自更新用的提交
 * 列表），而品牌通道一条提交都不发，于是走进「此安装方式无法显示更新日志」
 * 那句兜底文案。测的就是这条：清单上有说明时它必须出现在面板里。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { DesktopUpdateStatus } from '@/global'
import {
  $updateOverlayOpen,
  $updateOverlayTarget,
  $updateStatus,
  resetUpdateApplyState,
  setUpdateOverlayOpen
} from '@/store/updates'

import { UpdatesOverlay } from './updates-overlay'

const available = (extra: Partial<DesktopUpdateStatus> = {}): DesktopUpdateStatus => ({
  behind: 1,
  currentVersion: '0.17.12',
  fetchedAt: Date.now(),
  supported: true,
  targetVersion: '0.17.13',
  updateAvailable: true,
  updatePolicy: 'optional',
  ...extra
})

afterEach(() => {
  cleanup()
  setUpdateOverlayOpen(false)
  $updateStatus.set(null)
  $updateOverlayTarget.set('client')
  resetUpdateApplyState()
})

function open(status: DesktopUpdateStatus): HTMLElement {
  $updateStatus.set(status)
  $updateOverlayOpen.set(true)

  // The dialog portals out of the render container, so assertions read
  // baseElement (the whole document body) rather than the returned container.
  return render(<UpdatesOverlay />).baseElement as HTMLElement
}

describe('UpdatesOverlay 发行说明', () => {
  it('清单带说明时原样显示，不再说「无法显示更新日志」', () => {
    const notes = '修复后台调整模型清单后无法切换模型的问题。\n同日修复豆包多模态调用返回上游 404。'
    open(available({ releaseNotes: notes }))

    const block = screen.getByTestId('update-release-notes')

    expect(block.textContent).toContain('修复后台调整模型清单后无法切换模型的问题。')
    expect(block.textContent).toContain('豆包多模态调用返回上游 404')
  })

  it('换行照原样保留 —— 运维写的是多条，不能糊成一段', () => {
    open(available({ releaseNotes: '第一条\n第二条' }))

    const body = screen.getByTestId('update-release-notes').lastElementChild!

    expect(body.className).toContain('whitespace-pre-wrap')
    expect(body.textContent).toBe('第一条\n第二条')
  })

  it('有说明时不再并排显示写死的英文占位', () => {
    const body = open(available({ releaseNotes: '修了个问题' }))

    // buildCommitChangelog 在没有提交时回落到 "In this update /
    // Improvements and fixes"，那正是这次要替换掉的东西。
    expect(body.textContent).not.toContain('Improvements and fixes')
    expect(body.textContent).not.toContain('In this update')
  })

  it('清单没有说明时仍走上游的提交占位，不动上游行为', () => {
    const body = open(available())

    expect(screen.queryByTestId('update-release-notes')).toBeNull()
    expect(body.textContent).toContain('Improvements and fixes')
  })

  it('只有空白的说明当作没有', () => {
    open(available({ releaseNotes: '   \n  ' }))

    expect(screen.queryByTestId('update-release-notes')).toBeNull()
  })
})
