import { afterEach, describe, expect, it } from 'vitest'

import { setRuntimeI18nLocale } from '@/i18n'

import { localizeSlashDescription, localizeSlashGroup, slashCommandKey } from './slash-command-i18n'

const TABLE = { '/compress': '压缩对话上下文', '/new': '开始一个新会话' }

afterEach(() => setRuntimeI18nLocale('en'))

describe('slashCommandKey', () => {
  it('取第一个 token —— 带参数的命令仍要查得到', () => {
    expect(slashCommandKey('/skin list')).toBe('/skin')
    expect(slashCommandKey('/pet boba')).toBe('/pet')
  })

  it('非斜杠触发（@ 引用、emoji）不参与查表', () => {
    expect(slashCommandKey('@file:src/a.ts')).toBeNull()
    expect(slashCommandKey(':joy:')).toBeNull()
  })

  it('光一个斜杠不算命令', () => {
    expect(slashCommandKey('/')).toBeNull()
    expect(slashCommandKey('')).toBeNull()
  })

  it('不改大小写 —— 强行 lower 会让后端真发出来的怪名字查不到它自己', () => {
    expect(slashCommandKey('/Foo')).toBe('/Foo')
  })
})

describe('localizeSlashDescription', () => {
  it('命中就用中文', () => {
    expect(localizeSlashDescription('/compress', 'Compress conversation context', TABLE)).toBe('压缩对话上下文')
  })

  it('带参数时按命令名命中', () => {
    expect(localizeSlashDescription('/new here 5', 'Start a new session', TABLE)).toBe('开始一个新会话')
  })

  it('查不到回落到后端英文 —— 这张表不必跟命令表同步', () => {
    expect(localizeSlashDescription('/brand-new-upstream-cmd', 'Does a new thing', TABLE)).toBe('Does a new thing')
  })

  it('空表也不会把行弄没', () => {
    expect(localizeSlashDescription('/compress', 'Compress it', {})).toBe('Compress it')
  })

  it('后端也没给说明时返回空串，让调用方整行不渲染说明', () => {
    expect(localizeSlashDescription('/unknown', '', TABLE)).toBe('')
  })

  it('技能命令（不在内置表里）原样透传它自己的简介', () => {
    expect(localizeSlashDescription('/research-paper-writing', 'Write a paper', TABLE)).toBe('Write a paper')
  })
})

describe('localizeSlashGroup', () => {
  const GROUPS = { Session: '会话', 'Tools & Skills': '工具与技能' }

  it('翻译分组表头', () => {
    expect(localizeSlashGroup('Session', GROUPS)).toBe('会话')
    expect(localizeSlashGroup('Tools & Skills', GROUPS)).toBe('工具与技能')
  })

  it('没有对应翻译就原样显示', () => {
    expect(localizeSlashGroup('Experimental', GROUPS)).toBe('Experimental')
    expect(localizeSlashGroup('', GROUPS)).toBe('')
  })
})

describe('不传表时走运行时语言', () => {
  it('中文下拿到中文（/help 这类一次性文本输出走的就是这条路）', () => {
    setRuntimeI18nLocale('zh')
    expect(localizeSlashDescription('/compress', 'Compress conversation context')).toContain('压缩')
    expect(localizeSlashGroup('Session')).toBe('会话')
  })

  it('英文下回落到后端原文，而不是把 i18n key 漏到界面上', () => {
    setRuntimeI18nLocale('en')
    expect(localizeSlashDescription('/compress', 'Compress conversation context')).toBe(
      'Compress conversation context'
    )
    // 英文侧 slashGroups 是空表，原样返回——不能变成 'composer.slashGroups.Session'
    expect(localizeSlashGroup('Session')).toBe('Session')
  })
})
