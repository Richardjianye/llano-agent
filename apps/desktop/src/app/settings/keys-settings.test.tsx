import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import type { EnvVarInfo } from '@/types/hermes'

const getEnvVars = vi.fn()
const setEnvVar = vi.fn()

vi.mock('@/hermes', () => ({
  deleteEnvVar: vi.fn(),
  getEnvVars: () => getEnvVars(),
  revealEnvVar: vi.fn(),
  setEnvVar: (key: string, value: string) => setEnvVar(key, value)
}))

function toolKey(description: string): EnvVarInfo {
  return {
    advanced: false,
    category: 'tool',
    description,
    is_password: true,
    is_set: false,
    redacted_value: null,
    tools: [],
    url: null
  }
}

beforeEach(() => {
  getEnvVars.mockResolvedValue({
    FAL_KEY: toolKey('FAL API key for image and video generation'),
    KREA_API_KEY: toolKey('KREA API key for image generation')
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('KeysSettings 品牌能力策略', () => {
  it('中文工具列表保留视频生成密钥并隐藏已禁用的图片专用密钥', async () => {
    const { KeysSettings } = await import('./keys-settings')

    await act(async () => {
      render(
        <MemoryRouter>
          <I18nProvider configClient={null} initialLocale="zh">
            <KeysSettings view="tools" />
          </I18nProvider>
        </MemoryRouter>
      )
    })

    expect(await screen.findByText('FAL 生成密钥')).toBeTruthy()
    expect(screen.queryByText('Krea 生成密钥')).toBeNull()
  })
})

function customKey(isSet: boolean): EnvVarInfo {
  return {
    ...toolKey(''),
    category: 'custom',
    custom: true,
    is_set: isSet,
    redacted_value: isSet ? '••••' : null
  }
}

async function renderView(view: 'custom' | 'tools') {
  const { KeysSettings } = await import('./keys-settings')

  await act(async () => {
    render(
      <MemoryRouter>
        <I18nProvider configClient={null} initialLocale="zh">
          <KeysSettings view={view} />
        </I18nProvider>
      </MemoryRouter>
    )
  })
}

describe('validateCustomCredentialName', () => {
  const vars = { FAL_KEY: toolKey('FAL'), SHOP_TOKEN: customKey(true) }

  it('normalizes to an upper-case env name', async () => {
    const { validateCustomCredentialName } = await import('./keys-settings')

    expect(validateCustomCredentialName('  shop_api_token ', vars)).toEqual({ name: 'SHOP_API_TOKEN' })
  })

  it('rejects names that are not valid env var names', async () => {
    const { validateCustomCredentialName } = await import('./keys-settings')

    for (const raw of ['', '1TOKEN', 'MY-TOKEN', '店铺密钥', 'A B']) {
      expect(validateCustomCredentialName(raw, vars).error).toBe('invalid')
    }
  })

  it('refuses names the app owns so a custom key cannot shadow them', async () => {
    const { validateCustomCredentialName } = await import('./keys-settings')

    expect(validateCustomCredentialName('llano_gateway_api_key', vars).error).toBe('reserved')
    expect(validateCustomCredentialName('HERMES_HOME', vars).error).toBe('reserved')
  })

  it('sends catalog keys to their own card but lets custom keys be replaced', async () => {
    const { validateCustomCredentialName } = await import('./keys-settings')

    expect(validateCustomCredentialName('fal_key', vars).error).toBe('existing')
    expect(validateCustomCredentialName('SHOP_TOKEN', vars).error).toBeUndefined()
  })
})

describe('KeysSettings 自定义凭据', () => {
  it('lists set custom keys by their exact name and hides removed ones', async () => {
    getEnvVars.mockResolvedValue({
      FAL_KEY: toolKey('FAL API key for image and video generation'),
      GONE_TOKEN: customKey(false),
      SHOP_API_TOKEN: customKey(true)
    })

    await renderView('custom')

    expect(await screen.findByText('SHOP_API_TOKEN')).toBeTruthy()
    expect(screen.queryByText('GONE_TOKEN')).toBeNull()
    expect(screen.queryByText('FAL 生成密钥')).toBeNull()
  })

  it('saves a new custom credential through the env writer and lists it', async () => {
    getEnvVars.mockResolvedValue({})
    setEnvVar.mockResolvedValue({ ok: true })

    await renderView('custom')

    expect(await screen.findByText('还没有自定义凭据。')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'shop_api_token' } })
    fireEvent.change(screen.getByLabelText('值'), { target: { value: 'secret-value-123' } })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '保存' }))
    })

    expect(setEnvVar).toHaveBeenCalledWith('SHOP_API_TOKEN', 'secret-value-123')
    expect(await screen.findByText('SHOP_API_TOKEN')).toBeTruthy()
    expect((screen.getByLabelText('值') as HTMLInputElement).value).toBe('')
  })

  it('shows why a reserved name is refused without writing anything', async () => {
    getEnvVars.mockResolvedValue({})

    await renderView('custom')

    fireEvent.change(await screen.findByLabelText('名称'), { target: { value: 'LLANO_GATEWAY_API_KEY' } })
    fireEvent.change(screen.getByLabelText('值'), { target: { value: 'x' } })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '保存' }))
    })

    expect(screen.getByRole('alert').textContent).toContain('系统保留')
    expect(setEnvVar).not.toHaveBeenCalled()
  })
})
