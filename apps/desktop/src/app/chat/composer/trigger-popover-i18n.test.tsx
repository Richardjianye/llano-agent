/**
 * CUSTOM: 弹层确实走了中文表（接线回归）。
 *
 * `slash-command-i18n.test.ts` 测的是查表函数本身；这一条测的是**弹层真的调了它**。
 * 少了这条，哪天有人把 `description` 改回直接用 `meta?.meta`，函数级用例照样全绿，
 * 而界面上一夜之间又变回英文。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ComposerTriggerPopover } from './trigger-popover'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      composer: {
        lookupLoading: 'Loading…',
        lookupNoMatches: 'No matches',
        lookupOr: 'or',
        lookupTry: 'Try',
        slashCommandDescs: { '/compress': '压缩对话上下文' },
        slashGroups: { Session: '会话' }
      }
    }
  })
}))

function slashItem(command: string, group: string, meta = '') {
  return {
    id: `${command}|0`,
    label: command.slice(1),
    metadata: { action: '', command, display: command, group, meta, rawText: command },
    type: 'slash'
  }
}

const noop = () => {}

function renderPopover(items: ReturnType<typeof slashItem>[]) {
  return render(
    <ComposerTriggerPopover activeIndex={0} items={items} kind="/" loading={false} onHover={noop} onPick={noop} />
  )
}

describe('斜杠弹层的中文化', () => {
  it('命令说明显示中文，不显示后端下发的英文', () => {
    renderPopover([slashItem('/compress', 'Session', 'Compress conversation context')])

    expect(screen.getByText('压缩对话上下文')).toBeTruthy()
    expect(screen.queryByText('Compress conversation context')).toBeNull()
  })

  it('分组表头也翻译', () => {
    renderPopover([slashItem('/compress', 'Session', 'Compress conversation context')])

    expect(screen.getByText('会话')).toBeTruthy()
    expect(screen.queryByText('Session')).toBeNull()
  })

  it('表里没有的命令（上游新增、或用户自己的技能）原样显示英文，不变空白', () => {
    renderPopover([slashItem('/some-new-skill', 'Skills', 'Does a brand new thing')])

    expect(screen.getByText('Does a brand new thing')).toBeTruthy()
    // 分组没翻译也照常显示，不能吞掉表头
    expect(screen.getByText('Skills')).toBeTruthy()
  })
})
