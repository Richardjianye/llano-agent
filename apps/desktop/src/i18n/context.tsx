import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { getHermesConfigRecord, type HermesConfigRecord, saveHermesConfig } from '@/hermes'

import { TRANSLATIONS } from './catalog'
import { DEFAULT_LOCALE, localeConfigValue, normalizeLocale } from './languages'
import { setRuntimeI18nLocale } from './runtime'
import type { Locale, Translations } from './types'

export { LOCALE_META } from './languages'

export interface I18nConfigClient {
  getConfig: () => Promise<HermesConfigRecord>
  saveConfig: (config: HermesConfigRecord) => Promise<{ ok: boolean }>
}

const defaultConfigClient: I18nConfigClient = {
  getConfig: () => {
    if (typeof window === 'undefined' || !window.hermesDesktop?.api) {
      return Promise.resolve({})
    }

    return getHermesConfigRecord()
  },
  saveConfig: config => {
    if (typeof window === 'undefined' || !window.hermesDesktop?.api) {
      return Promise.resolve({ ok: true })
    }

    return saveHermesConfig(config)
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function getConfigDisplayLanguage(config: HermesConfigRecord): unknown {
  return isRecord(config.display) ? config.display.language : undefined
}

/**
 * 决定配置读回来之后用哪个语言。
 *
 * 「没配过」和「配了一个我们不发的语言」是两件事，以前这里混成了一件：
 * 无论哪种都走 normalizeLocale(undefined) → DEFAULT_LOCALE（英文）。
 *
 * 后果是全新安装的品牌构建**一定是英文的** —— main.tsx 传的 initialLocale='zh'
 * 只在 getConfig() **失败**时才活得下来，成功读到一份还没有 display.language
 * 的配置时反而被覆盖掉。员工装完看到英文界面，而切语言的入口在登录之后。
 *
 * 所以：没配过 → 用这个界面自己的默认语言；配了但不支持 → 仍按 DEFAULT_LOCALE
 * 处理，且不回写（以后真把那个语言加回来，用户原来的偏好还在）。
 */
export function resolveLoadedLocale(config: HermesConfigRecord, fallback: Locale): Locale {
  const configured = getConfigDisplayLanguage(config)

  if (configured == null || (typeof configured === 'string' && configured.trim() === '')) {
    return fallback
  }

  return normalizeLocale(configured)
}

export function withConfigDisplayLanguage(config: HermesConfigRecord, locale: Locale): HermesConfigRecord {
  const display = isRecord(config.display) ? config.display : {}

  return {
    ...config,
    display: {
      ...display,
      language: localeConfigValue(locale)
    }
  }
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error))
}

function applyDocumentLocale(locale: Locale) {
  if (typeof document === 'undefined') {
    return
  }

  document.documentElement.lang = locale
  // CUSTOM: 只发 zh + en（阶段 6），阿拉伯语移除之后没有任何 locale 是 RTL 的。
  // 这里原本有一张 RTL_LOCALES 表 —— 留一个恒假的判断比删掉更容易烂。以后要加
  // 回 RTL 语言，把那张表和这一行的三元一起加回来。
  document.documentElement.dir = 'ltr'
}

export interface I18nContextValue {
  configLoadError: Error | null
  isLoadingConfig: boolean
  isSavingLocale: boolean
  locale: Locale
  saveError: Error | null
  setLocale: (next: Locale) => Promise<void>
  t: Translations
}

const I18nContext = createContext<I18nContextValue>({
  configLoadError: null,
  isLoadingConfig: false,
  isSavingLocale: false,
  locale: DEFAULT_LOCALE,
  saveError: null,
  setLocale: async () => {},
  t: TRANSLATIONS[DEFAULT_LOCALE]
})

export interface I18nProviderProps {
  children: ReactNode
  configClient?: I18nConfigClient | null
  initialLocale?: unknown
}

export function I18nProvider({ children, configClient = defaultConfigClient, initialLocale }: I18nProviderProps) {
  const initialNormalizedLocale = normalizeLocale(initialLocale)
  const [locale, setLocaleState] = useState<Locale>(initialNormalizedLocale)
  const [isLoadingConfig, setIsLoadingConfig] = useState(false)
  const [isSavingLocale, setIsSavingLocale] = useState(false)
  const [configLoadError, setConfigLoadError] = useState<Error | null>(null)
  const [saveError, setSaveError] = useState<Error | null>(null)
  const localeRef = useRef(locale)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    localeRef.current = locale
    setRuntimeI18nLocale(locale)
    applyDocumentLocale(locale)
  }, [locale])

  useEffect(() => {
    if (!configClient) {
      return
    }

    let cancelled = false

    setIsLoadingConfig(true)
    setConfigLoadError(null)

    configClient
      .getConfig()
      .then(config => {
        if (!cancelled) {
          setLocaleState(resolveLoadedLocale(config, initialNormalizedLocale))
        }
      })
      .catch(error => {
        if (!cancelled) {
          setConfigLoadError(toError(error))
          // Keep the surface-specific initial locale when its backend config
          // cannot be read. This matters most for the branded login gate: a
          // stopped sidecar must not turn an actionable Chinese sign-out
          // message into an English fallback.
          setLocaleState(initialNormalizedLocale)
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoadingConfig(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [configClient, initialLocale, initialNormalizedLocale])

  const setLocale = useCallback(
    async (next: Locale) => {
      const previousLocale = localeRef.current

      setSaveError(null)
      setLocaleState(next)

      if (!configClient) {
        return
      }

      setIsSavingLocale(true)

      try {
        const latestConfig = await configClient.getConfig()
        const result = await configClient.saveConfig(withConfigDisplayLanguage(latestConfig, next))

        if (!result.ok) {
          throw new Error('Failed to save language')
        }
      } catch (error) {
        const nextError = toError(error)

        setLocaleState(previousLocale)
        setSaveError(nextError)

        throw nextError
      } finally {
        setIsSavingLocale(false)
      }
    },
    [configClient]
  )

  const value = useMemo<I18nContextValue>(
    () => ({
      configLoadError,
      isLoadingConfig,
      isSavingLocale,
      locale,
      saveError,
      setLocale,
      t: TRANSLATIONS[locale]
    }),
    [configLoadError, isLoadingConfig, isSavingLocale, locale, saveError, setLocale]
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  return useContext(I18nContext)
}
