import { QueryClient } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getGlobalModelOptions } from '@/hermes'

import {
  applyRefreshedModelOptions,
  manualPickRemoved,
  modelOptionsQueryKey,
  readCachedModelOptions,
  replacementModelFor,
  requestModelOptions
} from './model-options'

const globalOptions = { model: 'hermes-4', provider: 'nous', providers: [] }

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn(() => Promise.resolve(globalOptions))
}))

describe('requestModelOptions', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('uses the connected gateway even before a session exists', async () => {
    const gatewayPayload = { model: 'BeastMode', provider: 'moa', providers: [] }

    const gateway = {
      request: vi.fn(() => Promise.resolve(gatewayPayload))
    }

    await expect(requestModelOptions({ gateway: gateway as never, sessionId: null })).resolves.toBe(gatewayPayload)

    expect(gateway.request).toHaveBeenCalledWith('model.options', { explicit_only: true })
    expect(getGlobalModelOptions).not.toHaveBeenCalled()
  })

  it('passes the active session id and refresh flag through the gateway', async () => {
    const gateway = {
      request: vi.fn(() => Promise.resolve(globalOptions))
    }

    await requestModelOptions({ gateway: gateway as never, refresh: true, sessionId: 'session-1' })

    expect(gateway.request).toHaveBeenCalledWith('model.options', {
      explicit_only: true,
      refresh: true,
      session_id: 'session-1'
    })
  })

  it('falls back to REST when no gateway is connected', async () => {
    await requestModelOptions({ refresh: true })

    expect(getGlobalModelOptions).toHaveBeenCalledWith({ explicitOnly: true, refresh: true })
  })
})

describe('modelOptionsQueryKey', () => {
  it('isolates new-chat catalogs by active gateway profile', () => {
    expect(modelOptionsQueryKey('default')).toEqual(['model-options', 'default', 'global'])
    expect(modelOptionsQueryKey('compass')).toEqual(['model-options', 'compass', 'global'])
    expect(modelOptionsQueryKey('default')).not.toEqual(modelOptionsQueryKey('compass'))
  })

  it('keeps session catalogs inside the owning profile namespace', () => {
    expect(modelOptionsQueryKey(' compass ', 'session-1')).toEqual(['model-options', 'compass', 'session-1'])
  })
})

describe('manualPickRemoved', () => {
  const providers = [
    { name: 'OpenRouter', slug: 'openrouter', models: ['owl-alpha', 'gpt-5.5'] },
    { name: 'Nous', slug: 'nous', models: [] } // present but unconfigured / re-auth
  ]

  it('flags a pick whose model was dropped from a populated provider', () => {
    expect(manualPickRemoved(providers, 'openrouter', 'nemotron-removed')).toBe(true)
  })

  it('keeps a pick that is still in the catalog', () => {
    expect(manualPickRemoved(providers, 'openrouter', 'gpt-5.5')).toBe(false)
  })

  it('matches the provider by name as well as slug', () => {
    expect(manualPickRemoved(providers, 'OpenRouter', 'gpt-5.5')).toBe(false)
    expect(manualPickRemoved(providers, 'OpenRouter', 'gone')).toBe(true)
  })

  it('never clobbers when the provider is absent (ambiguous / deauth)', () => {
    expect(manualPickRemoved(providers, 'anthropic', 'claude-sonnet-4.6')).toBe(false)
  })

  it('never clobbers when the provider has an empty model list (re-auth)', () => {
    expect(manualPickRemoved(providers, 'nous', 'hermes-4')).toBe(false)
  })

  it('never clobbers on a not-yet-loaded or empty catalog', () => {
    expect(manualPickRemoved(undefined, 'openrouter', 'gpt-5.5')).toBe(false)
    expect(manualPickRemoved([], 'openrouter', 'gpt-5.5')).toBe(false)
  })

  it('never clobbers when there is no pick', () => {
    expect(manualPickRemoved(providers, '', '')).toBe(false)
  })
})

describe('replacementModelFor', () => {
  const providers = [
    { name: 'llano-gateway', slug: 'llano-gateway', models: ['deepseek-v4-flash', 'doubao-seed-2-0-lite-260428'] },
    { name: 'Nous', slug: 'nous', models: [] }
  ]

  it('returns the curated first model when the pick was revoked', () => {
    expect(replacementModelFor(providers, 'llano-gateway', 'deepseek-v4-flash-vision-exp')).toBe('deepseek-v4-flash')
  })

  it('returns null while the pick is still granted', () => {
    expect(replacementModelFor(providers, 'llano-gateway', 'deepseek-v4-flash')).toBeNull()
  })

  it('never jumps to another provider to rescue a send', () => {
    // 'nous' has nothing left; the llano-gateway models are NOT a valid target.
    expect(replacementModelFor(providers, 'nous', 'hermes-4')).toBeNull()
  })

  it('returns null on a not-yet-loaded catalog rather than guessing', () => {
    expect(replacementModelFor(undefined, 'llano-gateway', 'anything')).toBeNull()
  })

  it('prefers the verified default over the catalog order when it is still granted', () => {
    // 2026-09-18: the gateway listed Doubao first; a revoked DeepSeek pick must
    // land on the account's DeepSeek default, not jump vendors.
    const gateway = [
      { name: 'llano-gateway', slug: 'llano-gateway', models: ['doubao-seed-2-0-lite-260428', 'deepseek-v4.1-flash'] }
    ]

    expect(replacementModelFor(gateway, 'llano-gateway', 'deepseek-v4-flash', 'deepseek-v4.1-flash')).toBe(
      'deepseek-v4.1-flash'
    )
    expect(replacementModelFor(gateway, 'llano-gateway', 'deepseek-v4-flash', 'not-granted')).toBe(
      'doubao-seed-2-0-lite-260428'
    )
  })
})

describe('readCachedModelOptions', () => {
  const sessionPayload = { model: 'a', provider: 'llano-gateway', providers: [] }
  const globalPayload = { model: 'b', provider: 'llano-gateway', providers: [] }

  const clientWith = (entries: Array<[readonly unknown[], unknown]>) => ({
    getQueryData: (key: readonly unknown[]) => entries.find(([k]) => JSON.stringify(k) === JSON.stringify(key))?.[1]
  })

  it('prefers the session-scoped entry the in-chat picker writes', () => {
    const client = clientWith([
      [modelOptionsQueryKey('default', 's1'), sessionPayload],
      [modelOptionsQueryKey('default'), globalPayload]
    ])

    expect(readCachedModelOptions(client as any, 'default', 's1')).toBe(sessionPayload)
  })

  it('falls back to the global entry for a session whose picker never opened', () => {
    const client = clientWith([[modelOptionsQueryKey('default'), globalPayload]])

    expect(readCachedModelOptions(client as any, 'default', 's1')).toBe(globalPayload)
  })

  it('reads the global entry when there is no session', () => {
    const client = clientWith([[modelOptionsQueryKey('default'), globalPayload]])

    expect(readCachedModelOptions(client as any, 'default', null)).toBe(globalPayload)
  })
})

describe('applyRefreshedModelOptions', () => {
  const stale = {
    model: 'm',
    provider: 'llano-gateway',
    providers: [{ name: 'g', slug: 'llano-gateway', models: ['a', 'revoked'] }]
  }

  const fresh = {
    model: 'm',
    provider: 'llano-gateway',
    providers: [{ name: 'g', slug: 'llano-gateway', models: ['a'] }]
  }

  it('一个比刷新晚落地的普通请求不能把旧清单写回去', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: 60_000 } } })
    const key = modelOptionsQueryKey('default', 's1')

    // 菜单打开时发出的普通请求（不带 refresh），故意比刷新更晚返回
    let resolveMount!: (v: typeof stale) => void

    const mountFetch = client
      .fetchQuery({
        queryKey: key,
        queryFn: () => new Promise<typeof stale>(resolve => (resolveMount = resolve))
      })
      .catch(() => undefined)

    await applyRefreshedModelOptions(client, key, async () => fresh)
    resolveMount(stale)
    await mountFetch

    expect(client.getQueryData(key)).toEqual(fresh)
  })

  it('其他界面的旧清单被标记为过期，不用再等 60 秒', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: 60_000 } } })
    const clicked = modelOptionsQueryKey('default', 's1')
    const composer = modelOptionsQueryKey('default')

    client.setQueryData(composer, stale)
    await applyRefreshedModelOptions(client, clicked, async () => fresh)

    expect(client.getQueryState(composer)?.isInvalidated).toBe(true)
    expect(client.getQueryState(clicked)?.isInvalidated).toBe(false)
  })
})
