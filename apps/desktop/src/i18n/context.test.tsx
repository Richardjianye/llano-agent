import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesConfigRecord } from '@/hermes'

import { type I18nConfigClient, I18nProvider, useI18n } from './context'
import type { Locale } from './types'

function LanguageProbe({ target = 'zh' }: { target?: Locale }) {
  const { isLoadingConfig, isSavingLocale, locale, saveError, setLocale, t } = useI18n()

  return (
    <div>
      <p data-testid="locale">{locale}</p>
      <p data-testid="label">{t.language.label}</p>
      <p data-testid="save">{t.common.save}</p>
      <p data-testid="loading">{String(isLoadingConfig)}</p>
      <p data-testid="saving">{String(isSavingLocale)}</p>
      <p data-testid="save-error">{saveError?.message ?? ''}</p>
      <button onClick={() => void setLocale(target).catch(() => undefined)} type="button">
        switch
      </button>
    </div>
  )
}

describe('I18nProvider', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('defaults to English without a config client', () => {
    render(
      <I18nProvider configClient={null}>
        <LanguageProbe />
      </I18nProvider>
    )

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('label').textContent).toBe('Language')
  })

  it('normalizes an initial locale alias and switches translations', async () => {
    render(
      <I18nProvider configClient={null} initialLocale="zh-CN">
        <LanguageProbe target="en" />
      </I18nProvider>
    )

    expect(screen.getByTestId('locale').textContent).toBe('zh')
    expect(screen.getByTestId('label').textContent).toBe('语言')

    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(screen.getByTestId('locale').textContent).toBe('en'))
    expect(screen.getByTestId('label').textContent).toBe('Language')
  })

  it('loads the initial locale from display.language config', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'zh-Hans' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('zh')
    expect(screen.getByTestId('label').textContent).toBe('语言')
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  // 这条钉的是全新安装：配置**读得到**，但里面还没有 display.language。
  // 以前这里会被 normalizeLocale(undefined) 打成英文，于是品牌构建装完第一眼
  // 是英文界面 —— 而切语言的入口在登录之后，员工根本走不到。
  it('全新安装（配置里没有 display.language）用界面自己的默认语言', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ terminal: { cwd: '/home' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient} initialLocale="zh">
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('zh')
    expect(screen.getByTestId('label').textContent).toBe('语言')
    // 不回写：用户没选过语言，我们不替他把一个值钉进配置里。
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  // 上一条的反面，钉住上游行为没被改宽：不传 initialLocale 时仍是英文。
  it('没有界面默认语言时，缺 display.language 仍落到英文', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({}),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('label').textContent).toBe('Language')
  })

  // display.language 是空串 —— 配置文件被手改过或写了一半，等同于「没配」。
  it('display.language 为空串时按没配过处理', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: '   ' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient} initialLocale="zh">
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('zh')
  })

  it('keeps the initial locale usable when config loading fails', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockRejectedValue(new Error('config unavailable')),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient} initialLocale="zh">
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('zh')
    expect(screen.getByTestId('label').textContent).toBe('语言')
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  // 产品收敛到 zh + en（阶段 6）之后，zh-TW 归到简中而不是掉回英文。
  it('繁中的 display.language 配置落到简中', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'zh-TW' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient} initialLocale="zh">
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('zh')
    expect(screen.getByTestId('save').textContent).toBe('保存')
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  // 日语已不再发布。这条钉的是「不再支持的语言按不支持处理，且**不覆盖**用户
  // 已保存的配置」—— 以后真把它加回来，用户原来的偏好还在。
  it('已停发的语言落回默认语言，且不覆盖用户配置', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'ja-JP' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('save').textContent).toBe('Save')
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  it('does not overwrite unsupported configured languages', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'de' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient} initialLocale="zh">
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('label').textContent).toBe('Language')
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  it('reads latest config before saving language and preserves unrelated values', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true })

    const latestConfig: HermesConfigRecord = {
      display: { language: 'en', skin: 'slate' },
      terminal: { cwd: '/new' }
    }

    const configClient: I18nConfigClient = {
      getConfig: vi
        .fn()
        .mockResolvedValueOnce({ display: { language: 'en', skin: 'mono' }, terminal: { cwd: '/old' } })
        .mockResolvedValueOnce(latestConfig),
      saveConfig
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))
    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    expect(saveConfig).toHaveBeenCalledWith({
      display: { language: 'zh', skin: 'slate' },
      terminal: { cwd: '/new' }
    })
  })

  it('切换语言会写回 display.language', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true })

    const configClient: I18nConfigClient = {
      getConfig: vi
        .fn()
        .mockResolvedValueOnce({ display: { language: 'en' } })
        .mockResolvedValueOnce({ display: { language: 'en', skin: 'mono' } }),
      saveConfig
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe target="zh" />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))
    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    expect(saveConfig).toHaveBeenCalledWith({ display: { language: 'zh', skin: 'mono' } })
    expect(screen.getByTestId('locale').textContent).toBe('zh')
  })

  it('rolls back the visible locale when saving fails', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'en' } }),
      saveConfig: vi.fn().mockRejectedValue(new Error('save failed'))
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))
    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(screen.getByTestId('save-error').textContent).toBe('save failed'))

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('label').textContent).toBe('Language')
  })
})
