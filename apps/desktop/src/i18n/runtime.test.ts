import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { fieldCopyForSchemaKey } from '@/app/settings/field-copy'
import { PRODUCT_POLICY } from '@/lib/product-policy'

import { TRANSLATIONS } from './catalog'
import { setRuntimeI18nLocale, translateNow } from './runtime'
import { zh } from './zh'

describe('desktop i18n runtime translator', () => {
  beforeEach(() => {
    setRuntimeI18nLocale('en')
  })

  afterEach(() => {
    setRuntimeI18nLocale('en')
  })

  it('translates string paths for the active runtime locale', () => {
    setRuntimeI18nLocale('zh')

    expect(translateNow('boot.ready')).toBe(`${PRODUCT_POLICY.productName}已就绪`)
    expect(translateNow('notifications.voice.noSpeechDetected')).toBe('没有检测到语音')
    expect(translateNow('composer.lookupNoMatches')).toBe('没有匹配项。')
    expect(translateNow('assistant.tool.statusRecovered')).toBe('已恢复')
  })

  it('passes arguments to function translations', () => {
    expect(translateNow('notifications.updateReadyMessage', 2)).toBe('2 new changes available.')
  })

  it('keeps translated settings field copy addressable from schema keys', () => {
    const field = ['display', 'show_reasoning'].join('.')

    expect(fieldCopyForSchemaKey(zh.settings.fieldLabels, field)).toBe('推理过程块')
    expect(fieldCopyForSchemaKey(zh.settings.fieldDescriptions, field)).toBe('当后端提供推理内容时予以显示。')
  })

  it('falls back to English when the active locale cannot resolve a key', () => {
    const boot = TRANSLATIONS.zh.boot as { ready?: string }
    const originalReady = boot.ready

    try {
      boot.ready = undefined
      setRuntimeI18nLocale('zh')

      expect(translateNow('boot.ready')).toBe(`${PRODUCT_POLICY.productName} is ready`)
    } finally {
      boot.ready = originalReady
    }
  })

  it('returns the key when no locale can resolve a path', () => {
    setRuntimeI18nLocale('zh')

    expect(translateNow('missing.path')).toBe('missing.path')
  })
})
