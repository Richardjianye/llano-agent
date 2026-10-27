import { type QueryClient } from '@tanstack/react-query'

import { getGlobalModelOptions, type HermesGateway, type ModelOptionsResponse } from '@/hermes'
import type { ModelOptionProvider } from '@/types/hermes'

/**
 * True only when a persisted **manual** composer pick has been removed from the
 * catalog (its provider still ships models, but no longer this one) — so a new
 * chat would keep 404'ing the dead model. Deliberately conservative to never
 * clobber a still-valid pick: an unknown/absent provider, an empty model list
 * (re-auth / unconfigured), or a not-yet-loaded catalog all return false.
 */
export function manualPickRemoved(
  providers: ModelOptionProvider[] | undefined,
  provider: string,
  model: string
): boolean {
  if (!providers?.length || !provider || !model) {
    return false
  }

  const row = providers.find(p => p.slug === provider || p.name === provider)

  if (!row) {
    return false
  }

  const models = row.models ?? []

  // Empty list means the provider is present but unconfigured / awaiting
  // re-auth, not that the model was dropped — leave the pick alone.
  if (models.length === 0) {
    return false
  }

  return !models.includes(model)
}

interface ModelOptionsRequest {
  /** When false, include ambient/unconfigured providers (onboarding/setup
   *  surfaces). Chat pickers default to true so only explicitly configured
   *  providers are listed (#56974). */
  explicitOnly?: boolean
  gateway?: HermesGateway
  refresh?: boolean
  sessionId?: null | string
}

export function modelOptionsQueryKey(profile: null | string | undefined, sessionId?: null | string) {
  const profileKey = (profile ?? '').trim() || 'default'

  return ['model-options', profileKey, sessionId || 'global'] as const
}

export function requestModelOptions({
  explicitOnly = true,
  gateway,
  refresh = false,
  sessionId
}: ModelOptionsRequest): Promise<ModelOptionsResponse> {
  if (gateway) {
    const params: Record<string, unknown> = {}

    if (sessionId) {
      params.session_id = sessionId
    }

    if (refresh) {
      params.refresh = true
    }

    if (explicitOnly) {
      params.explicit_only = true
    }

    return gateway.request<ModelOptionsResponse>('model.options', params)
  }

  return getGlobalModelOptions({ explicitOnly, ...(refresh ? { refresh: true } : {}) })
}

/**
 * The model a session should fall back to when its pick was revoked.
 *
 * Same provider only. A cross-provider jump would silently move the employee
 * onto a different upstream (different price, different data path) to rescue a
 * single send — that is a decision for the user, not a repair. When the
 * provider has nothing left, returns `null` and the caller surfaces the
 * existing "no models" states instead of inventing a target.
 *
 * Returns `null` whenever the pick is still valid, so callers can treat a
 * non-null result as "switch to this".
 */
export function replacementModelFor(
  providers: ModelOptionProvider[] | undefined,
  provider: string,
  model: string,
  preferred?: null | string
): null | string {
  if (!manualPickRemoved(providers, provider, model)) {
    return null
  }

  const row = providers?.find(p => p.slug === provider || p.name === provider)

  // A caller that already knows the account's verified default (the branded
  // send gate) passes it as `preferred`: the managed gateway's catalog order is
  // not a relevance order — on 2026-09-18 it listed Doubao first, which would
  // have moved DeepSeek conversations onto a different vendor.
  if (preferred && row?.models?.includes(preferred)) {
    return preferred
  }

  // Otherwise the catalog order is the backend's curated relevance order, so the
  // first entry is the closest thing to "what this person should be using now".
  return row?.models?.[0] ?? null
}

/**
 * Read whichever cached catalog actually describes *sessionId*.
 *
 * The in-chat picker writes the session-scoped key while the composer's
 * reseed path writes the profile-global one. Reading only the global key is
 * what let a live session keep offering a model the gateway had already
 * revoked: the fresh catalog sat in the session entry the whole time, in a
 * slot nobody compared against. Session first, global as the fallback for a
 * session whose picker was never opened.
 */
export function readCachedModelOptions(
  queryClient: QueryClient,
  profile: null | string | undefined,
  sessionId?: null | string
): ModelOptionsResponse | undefined {
  const scoped = sessionId
    ? queryClient.getQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile, sessionId))
    : undefined

  return scoped ?? queryClient.getQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile))
}

/**
 * Land an explicitly refreshed catalog on EVERY surface, not just the one whose
 * refresh button was clicked.
 *
 * The desktop QueryClient defaults to `staleTime: 60_000`, and the catalog is
 * cached under one key per surface (profile-global, and one per session/tile).
 * Writing only the clicked surface's key had two holes, both of which read to a
 * user as "refresh does nothing, then it fixes itself a minute later":
 *
 * - an in-flight mount fetch for that same key (issued when the menu opened,
 *   without `refresh`) could resolve AFTER our write and put the old list back;
 * - every other surface kept its stale copy until its own 60s ran out.
 *
 * So: cancel in-flight fetches first, write the fresh payload, then mark all
 * other catalog keys stale. Their refetch is cheap and correct now — the
 * refresh already rebuilt the backend's catalog cache.
 */
export async function applyRefreshedModelOptions(
  queryClient: QueryClient,
  queryKey: readonly unknown[],
  fetchFresh: () => Promise<ModelOptionsResponse>
): Promise<ModelOptionsResponse> {
  await queryClient.cancelQueries({ queryKey: ['model-options'] })

  const next = await fetchFresh()

  queryClient.setQueryData<ModelOptionsResponse>(queryKey, next)

  const target = JSON.stringify(queryKey)

  await queryClient.invalidateQueries({
    predicate: query => query.queryKey[0] === 'model-options' && JSON.stringify(query.queryKey) !== target
  })

  return next
}
