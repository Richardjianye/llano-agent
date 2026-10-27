import { describe, expect, it } from 'vitest'

import { DEFAULT_LOCALE, isLocale, isSupportedLocaleValue, localeConfigValue, normalizeLocale } from './languages'

describe('desktop i18n languages', () => {
  it('normalizes supported locale aliases', () => {
    expect(normalizeLocale('en')).toBe('en')
    expect(normalizeLocale('EN-US')).toBe('en')
    expect(normalizeLocale('zh')).toBe('zh')
    expect(normalizeLocale('zh-CN')).toBe('zh')
    expect(normalizeLocale('zh-Hans')).toBe('zh')
    expect(normalizeLocale(' zh_hans_cn ')).toBe('zh')
  })

  // 产品只发 zh + en（阶段 6）。繁中系统语言归到简中而不是掉回英文 ——
  // 对一个台港澳的中文用户来说，英文界面是更差的结果。
  it('繁中的系统语言归到简中，而不是掉回英文', () => {
    expect(normalizeLocale('zh-Hant')).toBe('zh')
    expect(normalizeLocale('zh-TW')).toBe('zh')
    expect(normalizeLocale('zh_HK')).toBe('zh')
    expect(normalizeLocale('zh-MO')).toBe('zh')
  })

  // 日语和阿拉伯语的别名整条删掉了，落到 DEFAULT_LOCALE。这条钉住的是
  // 「不再假装支持」—— 留着别名而没有对应的翻译包，界面会是一半英文一半空。
  it('不再发的语言落回默认语言', () => {
    expect(normalizeLocale('ja')).toBe(DEFAULT_LOCALE)
    expect(normalizeLocale('ja-JP')).toBe(DEFAULT_LOCALE)
    expect(normalizeLocale('ar')).toBe(DEFAULT_LOCALE)
    expect(normalizeLocale('AR-SA')).toBe(DEFAULT_LOCALE)
  })

  it('falls back to English for empty or unsupported values', () => {
    expect(normalizeLocale(null)).toBe(DEFAULT_LOCALE)
    expect(normalizeLocale('')).toBe(DEFAULT_LOCALE)
    expect(normalizeLocale('de')).toBe(DEFAULT_LOCALE)
  })

  it('distinguishes exact locale ids from supported config aliases', () => {
    expect(isSupportedLocaleValue('zh-CN')).toBe(true)
    expect(isSupportedLocaleValue('zh-TW')).toBe(true)
    expect(isSupportedLocaleValue('ja-JP')).toBe(false)
    expect(isSupportedLocaleValue('de')).toBe(false)
    expect(isLocale('zh-CN')).toBe(false)
    expect(isLocale('zh')).toBe(true)
    expect(isLocale('en')).toBe(true)
    expect(isLocale('zh-hant')).toBe(false)
    expect(isLocale('ja')).toBe(false)
  })

  it('returns the persisted config value for supported locales', () => {
    expect(localeConfigValue('en')).toBe('en')
    expect(localeConfigValue('zh')).toBe('zh')
  })
})
