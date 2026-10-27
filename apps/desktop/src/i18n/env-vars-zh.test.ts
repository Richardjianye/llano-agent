import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { cleanup, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CredentialKeyCard, credentialRowLabel } from '@/app/settings/credential-key-ui'
import type { EnvVarInfo } from '@/types/hermes'

import { I18nProvider } from './context'
import { ENV_VAR_ZH, localizeEnvVar } from './env-vars.zh'

const sudoInfo = {
  advanced: false,
  category: 'setting',
  description:
    'Sudo password for terminal commands requiring root access; set to an explicit empty string to try empty without prompting',
  is_password: true,
  is_set: false,
  redacted_value: null,
  tools: [],
  url: null
} satisfies EnvVarInfo

interface BackendEnvVar {
  category: string
  key: string
}

function readBackendEnvVars(): BackendEnvVar[] {
  const source = readFileSync(resolve(process.cwd(), '../../hermes_cli/config_defaults.py'), 'utf8')
  const tableStart = source.indexOf('OPTIONAL_ENV_VARS = {')

  if (tableStart < 0) {
    throw new Error('OPTIONAL_ENV_VARS static table not found')
  }

  const entries: BackendEnvVar[] = []
  const entryPattern = /^ {4}"([A-Z0-9_]+)": \{\n([\s\S]*?)^ {4}\},$/gm

  for (const match of source.slice(tableStart).matchAll(entryPattern)) {
    const category = match[2].match(/^\s+"category": "([a-z]+)",$/m)?.[1]

    if (category) {
      entries.push({ category, key: match[1] })
    }
  }

  if (!entries.some(entry => entry.key === 'SUDO_PASSWORD')) {
    throw new Error('OPTIONAL_ENV_VARS parser did not find SUDO_PASSWORD')
  }

  return entries
}

const backendEnvVars = readBackendEnvVars()

describe('环境变量中文文案', () => {
  afterEach(cleanup)

  it('中文界面的主列表标题使用短中文提示', () => {
    expect(credentialRowLabel('SUDO_PASSWORD', sudoInfo, 'zh')).toBe('终端提权密码')
  })

  it('中文界面的展开卡片显示中文说明', () => {
    render(
      createElement(I18nProvider, {
        children: createElement(CredentialKeyCard, {
          expanded: true,
          info: sudoInfo,
          label: '终端提权密码',
          onExpand: vi.fn(),
          onToggle: vi.fn(),
          placeholder: '终端提权密码',
          rowProps: {
            edits: {},
            onClear: vi.fn(),
            onReveal: vi.fn(),
            onSave: vi.fn(),
            revealed: {},
            saving: null,
            setEdits: vi.fn()
          },
          varKey: 'SUDO_PASSWORD'
        }),
        configClient: null,
        initialLocale: 'zh'
      })
    )

    expect(
      screen.getByText('终端命令需要管理员权限时使用的提权密码；密码为空时也可明确保存空值，避免再次询问')
    ).not.toBeNull()
  })

  it('覆盖表里的键全部来自后端静态表', () => {
    const backendKeys = new Set(backendEnvVars.map(entry => entry.key))
    const unknownKeys = Object.keys(ENV_VAR_ZH).filter(key => !backendKeys.has(key))

    expect(unknownKeys).toEqual([])
  })

  it('工具、设置和消息类静态条目都有完整中文文案', () => {
    // channel_managed 是运行时结果；覆盖整个 messaging 静态集合，保证任何
    // 未被消息平台页接管、因而落到「工具与密钥」的行都不会中英混排。
    const scoped = backendEnvVars.filter(entry => ['tool', 'setting', 'messaging'].includes(entry.category))

    const missing = scoped.filter(({ key }) => {
      const copy = ENV_VAR_ZH[key]

      return !copy?.description.trim() || !copy.prompt?.trim()
    })

    expect(missing).toEqual([])
  })

  it('覆盖文案非空、包含中文，且不出现品牌禁用词或外部网址', () => {
    for (const [key, copy] of Object.entries(ENV_VAR_ZH)) {
      expect(copy.description, key).toMatch(/[\u3400-\u9fff]/)
      expect(copy.prompt, key).toMatch(/[\u3400-\u9fff]/)
      expect(`${copy.prompt ?? ''}${copy.description}`, key).not.toMatch(/Hermes|Nous|https?:\/\/|www\./i)
      expect([...(copy.prompt?.match(/\p{Script=Han}/gu) ?? [])].length, key).toBeLessThanOrEqual(10)
    }
  })

  it('英文 locale 和未知动态条目原样保留后端文案', () => {
    const backend = {
      description: 'Dynamic backend description',
      prompt: 'Dynamic backend prompt'
    }

    expect(localizeEnvVar('en', 'SUDO_PASSWORD', backend)).toEqual(backend)
    expect(localizeEnvVar('zh', 'DYNAMIC_PLUGIN_KEY', backend)).toEqual(backend)
  })
})
