import { type FormEvent, useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n'
import { Loader2, Save } from '@/lib/icons'
import { isEnvVarAllowed } from '@/lib/product-policy'
import type { EnvVarInfo } from '@/types/hermes'

import { CREDENTIAL_CONTROL_CLASS, CredentialKeyCard, credentialPlaceholder, credentialRowLabel } from './credential-key-ui'
import { useEnvCredentials } from './env-credentials'
import { asText } from './helpers'
import { SettingsContent, SettingsSkeleton } from './primitives'
import { useDeepLinkHighlight } from './use-deep-link-highlight'

// Sub-views surfaced as sidebar subnav under Tools & Keys (see settings/index.tsx).
// `custom` is the branded desktop's only credential entry point for keys no
// catalog knows (employees have no CLI); the nav shows it only when branded.
export const KEYS_VIEWS = ['tools', 'settings', 'custom'] as const

export type KeysView = (typeof KEYS_VIEWS)[number]

// Providers live on their own page; messaging-platform credentials live on the
// dedicated Messaging page (and are hidden here via `channel_managed`). This
// view covers tool API keys plus server/setting env vars (API server, webhook,
// gateway), which fold into the Settings subnav.

// Backend categories that surface under each subnav. Platform credentials use the
// `messaging` category but are flagged ``channel_managed`` and configured on
// the Messaging page; only gateway-wide ``messaging`` rows (e.g. GATEWAY_PROXY)
// appear here alongside ``setting``.
const VIEW_CATEGORIES: Record<KeysView, readonly string[]> = {
  custom: ['custom'],
  settings: ['setting', 'messaging'],
  tools: ['tool']
}

const CUSTOM_NAME_PATTERN = /^[A-Z_][A-Z0-9_]*$/

// Names the app itself owns: the sidecar's gateway key and Hermes runtime
// settings. `.env` loads with override, so a planted copy would shadow them.
const RESERVED_CUSTOM_PREFIXES = ['HERMES_', 'LLANO_']

export type CustomCredentialNameError = 'existing' | 'invalid' | 'reserved'

/** Normalize a user-typed credential name and say why it can't be added, if so. */
export function validateCustomCredentialName(
  raw: string,
  vars: Record<string, EnvVarInfo>
): { error?: CustomCredentialNameError; name: string } {
  const name = raw.trim().toUpperCase()

  if (!CUSTOM_NAME_PATTERN.test(name)) {
    return { error: 'invalid', name }
  }

  if (RESERVED_CUSTOM_PREFIXES.some(prefix => name.startsWith(prefix))) {
    return { error: 'reserved', name }
  }

  const existing = vars[name]

  if (existing && !existing.custom) {
    return { error: 'existing', name }
  }

  return { name }
}

export function KeysSettings({ view }: KeysSettingsProps) {
  const { locale, t } = useI18n()
  const { rowProps, saveValue, vars } = useEnvCredentials()
  const [openKey, setOpenKey] = useState<null | string>(null)

  useEffect(() => {
    setOpenKey(null)
  }, [view])

  // Deep link from Capabilities env-var rows (?tab=keys&key=<ENV_KEY>): scroll
  // the credential card into view, flash it, and expand it. Same mechanism the
  // command palette uses for config fields / archived sessions.
  useDeepLinkHighlight({
    elementId: key => `credential-key-${key}`,
    onResolve: key => setOpenKey(key),
    param: 'key',
    ready: key => Boolean(vars && key in vars)
  })

  const groups = useMemo(() => {
    if (!vars) {
      return []
    }

    return KEYS_VIEWS.flatMap(v => {
      const cats = VIEW_CATEGORIES[v]

      const entries = Object.entries(vars)
        .filter(
          ([key, info]) =>
            isEnvVarAllowed(key) &&
            !info.channel_managed &&
            cats.includes(asText(info.category)) &&
            // A removed custom key has no catalog entry to fall back to.
            (!info.custom || info.is_set)
        )
        .sort(([a], [b]) => a.localeCompare(b))

      return entries.length === 0 && v !== 'custom' ? [] : [{ category: v, entries }]
    })
  }, [vars])

  if (!vars) {
    return <SettingsSkeleton sections={[{ rows: 5 }]} />
  }

  const visible = groups.filter(g => g.category === view)

  return (
    <SettingsContent>
      {view === 'custom' && (
        <CustomCredentialForm
          onSaved={key => setOpenKey(key)}
          saveValue={saveValue}
          saving={rowProps.saving}
          vars={vars}
        />
      )}
      {visible.map(group => (
        <div className="grid gap-2" key={group.category}>
          {group.entries.length === 0 && (
            <div className="rounded-lg border border-dashed border-(--ui-stroke-tertiary) px-4 py-8 text-center text-[length:var(--conversation-caption-font-size)] text-muted-foreground">
              {t.settings.keys.custom.empty}
            </div>
          )}
          {group.entries.map(([key, info]: [string, EnvVarInfo]) => {
            // Custom keys show their exact name: it is what the agent and the
            // skill instructions refer to.
            const label = info.custom ? key : credentialRowLabel(key, info, locale)

            return (
              <div className="scroll-mt-6 rounded-[6px]" id={`credential-key-${key}`} key={key}>
                <CredentialKeyCard
                  expanded={openKey === key}
                  info={info}
                  label={label}
                  onExpand={() => setOpenKey(key)}
                  onToggle={() => setOpenKey(prev => (prev === key ? null : key))}
                  placeholder={credentialPlaceholder(key, info, label)}
                  rowProps={rowProps}
                  varKey={key}
                />
              </div>
            )
          })}
        </div>
      ))}

      {visible.length === 0 && (
        <div className="rounded-lg border border-dashed border-(--ui-stroke-tertiary) px-4 py-8 text-center text-[length:var(--conversation-caption-font-size)] text-muted-foreground">
          {t.settings.keys.empty}
        </div>
      )}
    </SettingsContent>
  )
}

function CustomCredentialForm({ onSaved, saveValue, saving, vars }: CustomCredentialFormProps) {
  const { t } = useI18n()
  const copy = t.settings.keys.custom
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [error, setError] = useState<null | string>(null)
  const busy = saving !== null
  const ready = name.trim().length > 0 && value.trim().length > 0

  async function submit(e: FormEvent) {
    e.preventDefault()

    const checked = validateCustomCredentialName(name, vars)

    if (checked.error) {
      setError(
        checked.error === 'existing'
          ? copy.existingName(checked.name)
          : checked.error === 'reserved'
            ? copy.reservedName
            : copy.invalidName
      )

      return
    }

    const result = await saveValue(checked.name, value)

    if (!result.ok) {
      setError(result.message ?? null)

      return
    }

    setName('')
    setValue('')
    setError(null)
    onSaved(checked.name)
  }

  return (
    <form className="grid gap-2 rounded-[6px] p-3 ring-1 ring-(--ui-stroke-secondary)" onSubmit={e => void submit(e)}>
      <p className="text-[length:var(--conversation-caption-font-size)] text-muted-foreground">{copy.intro}</p>
      <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] items-center gap-2">
        <Input
          aria-label={copy.nameLabel}
          autoCapitalize="characters"
          className={CREDENTIAL_CONTROL_CLASS}
          onChange={e => {
            setName(e.target.value)
            setError(null)
          }}
          placeholder={copy.namePlaceholder}
          spellCheck={false}
          value={name}
        />
        <Input
          aria-label={copy.valueLabel}
          autoComplete="off"
          className={CREDENTIAL_CONTROL_CLASS}
          onChange={e => setValue(e.target.value)}
          placeholder={copy.valuePlaceholder}
          type="password"
          value={value}
        />
        <Button className="h-8" disabled={busy || !ready} size="sm" type="submit">
          {busy ? <Loader2 className="animate-spin" /> : <Save />}
          {copy.add}
        </Button>
      </div>
      {error && (
        <p className="text-[length:var(--conversation-caption-font-size)] text-destructive" role="alert">
          {error}
        </p>
      )}
    </form>
  )
}

interface CustomCredentialFormProps {
  onSaved: (key: string) => void
  saveValue: (key: string, value: string) => Promise<{ message?: string; ok: boolean }>
  saving: null | string
  vars: Record<string, EnvVarInfo>
}

interface KeysSettingsProps {
  view: KeysView
}
