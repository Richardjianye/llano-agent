import { type QueryClient } from '@tanstack/react-query'
import { useCallback, useRef } from 'react'

import type { ModelSelection } from '@/app/shell/model-menu-panel'
import type { ProductModelSendGate } from '@/global'
import { getGlobalModelInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { isBusySessionModelSwitch } from '@/lib/gateway-rpc'
import { modelOptionsQueryKey, readCachedModelOptions, replacementModelFor } from '@/lib/model-options'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $currentModel,
  $currentProvider,
  getComposerSelectionGeneration,
  getCurrentModelSource,
  markComposerSelectionManual,
  setCurrentModel,
  setCurrentModelSource,
  setCurrentProvider
} from '@/store/session'
import { $sessionStates, sessionTileDelegate } from '@/store/session-states'
import type { ModelOptionsResponse } from '@/types/hermes'

/** 与网关模型目录的服务端 TTL 对齐（`_MANAGED_MODELS_CACHE_TTL`）。对得比它
 *  密没有意义：那边也只会把同一份缓存再答一遍。 */
const RECONCILE_MIN_INTERVAL_MS = 60_000

/** The account's verified default from the branded send gate, if it answered. */
function readyModel(gate?: null | ProductModelSendGate): null | string {
  return gate?.status === 'ready' ? gate.model : null
}

interface ModelControlsOptions {
  queryClient: QueryClient
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}

export function useModelControls({ queryClient, requestGateway }: ModelControlsOptions) {
  const { t } = useI18n()
  const copy = t.desktop
  const profileRefreshEpochRef = useRef(0)
  // 上次对账时间（按会话）。发送路径每条消息都会问一次，而网关那边模型目录
  // 本来就有 60s TTL —— 比这更密的往返只是在给每条消息加一次空转。
  const reconciledAtRef = useRef<Record<string, number>>({})

  // All callbacks here read reactive session state from the store (.get())
  // rather than capturing it as a prop. The actions bag in wiring.tsx mutates
  // in place to keep a stable identity, so memoized surfaces capture these
  // callbacks once and never re-evaluate — a captured prop would be stale
  // forever. The store read is always current.
  const updateModelOptionsCache = useCallback(
    (
      sessionId: null | string,
      provider: string,
      model: string,
      includeGlobal: boolean,
      profile = $activeGatewayProfile.get()
    ) => {
      const patch = (prev: ModelOptionsResponse | undefined) => ({ ...(prev ?? {}), provider, model })

      queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile, sessionId), patch)

      if (includeGlobal) {
        queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile), patch)
      }
    },
    [queryClient]
  )

  // Settings → Model writes the profile default, which the backend applies to
  // new sessions only. Keep a live session's renderer state and session-scoped
  // model-options cache authoritative instead of briefly painting the saved
  // default as if the active agent had switched. Marking the composer as
  // default-derived still lets the next fresh draft reseed from profile config.
  const applySavedMainModel = useCallback(
    (provider: string, model: string) => {
      const liveSessionId = $activeSessionId.get()

      setCurrentModelSource('default')

      if (!liveSessionId) {
        setCurrentProvider(provider)
        setCurrentModel(model)
      }

      // A null session id is the profile-global model-options key. Never patch
      // the live session key here: only config.set --session may change it.
      updateModelOptionsCache(null, provider, model, false)
    },
    [updateModelOptionsCache]
  )

  // Seed the composer's model state from the profile default. `force` reseeds
  // for a profile swap (the new profile has its own default); otherwise this
  // only fills an EMPTY selection so a user's pick (plain UI state in
  // $currentModel) survives the lifecycle refreshes that fire on boot / fresh
  // draft / session events. A live session owns the footer, so skip entirely.
  const refreshCurrentModel = useCallback(
    async (force = false) => {
      // A forced profile swap opens a new intent epoch; an older in-flight
      // response for a previous profile must stand down when it resolves.
      if (force) {
        profileRefreshEpochRef.current += 1
      }

      const profileRefreshEpoch = profileRefreshEpochRef.current

      try {
        if ($activeSessionId.get()) {
          return
        }

        // A manual pick stays sticky UNLESS it was removed from the catalog (its
        // model no longer exists on the provider), in which case keeping it would
        // 404 every new chat — fall through to reseed from the profile default.
        // Reads the model-options cache the composer already populated; an
        // unknown/not-yet-loaded catalog conservatively preserves the pick.
        const keepManualPick = () => {
          if (force || !$currentModel.get() || getCurrentModelSource() !== 'manual') {
            return false
          }

          const options = readCachedModelOptions(queryClient, $activeGatewayProfile.get(), $activeSessionId.get())

          return !replacementModelFor(options?.providers, $currentProvider.get(), $currentModel.get())
        }

        if (keepManualPick()) {
          return
        }

        // Snapshot the selection generation before awaiting so a picker click
        // that lands while getGlobalModelInfo is in flight wins over this older
        // default — value comparisons alone miss re-selecting the same row.
        const selectionGeneration = getComposerSelectionGeneration()
        const result = await getGlobalModelInfo()

        if (
          profileRefreshEpochRef.current !== profileRefreshEpoch ||
          $activeSessionId.get() ||
          getComposerSelectionGeneration() !== selectionGeneration ||
          keepManualPick()
        ) {
          return
        }

        if (typeof result.model === 'string') {
          // The profile default is checked against the catalog too, not just a
          // manual pick: a default the administrator has since revoked would
          // otherwise seed every new chat with a model the gateway answers 403
          // for (2026-09-18, deepseek-v4-flash). The persistent fix — rewriting
          // the saved default — is the main process's send gate; this only keeps
          // the composer from painting and shipping the stale name meanwhile.
          const cached = readCachedModelOptions(queryClient, $activeGatewayProfile.get(), null)
          const provider = typeof result.provider === 'string' ? result.provider : $currentProvider.get()

          setCurrentModel(replacementModelFor(cached?.providers, provider, result.model) ?? result.model)
        }

        if (typeof result.provider === 'string') {
          setCurrentProvider(result.provider)
        }

        if (typeof result.model === 'string' || typeof result.provider === 'string') {
          setCurrentModelSource('default')
        }
      } catch {
        // The delayed session.info event still updates this once the agent is ready.
      }
    },
    [queryClient]
  )

  // Returns whether the switch succeeded so callers can await it before applying
  // follow-up changes. The composer model is plain UI state: with no live
  // session it's just stored (and shipped on the next session.create); with one
  // it's scoped to that session via config.set. It NEVER writes the profile
  // default — that lives in Settings → Model — so picking a model here can't
  // silently mutate global config.
  //
  // `selection.sessionId` targets a specific surface (tile). When omitted, the
  // primary `$activeSessionId` is used (overlay / legacy callers). A tile
  // switch must not touch the primary globals — and must not be blocked by a
  // busy primary turn.
  const selectModel = useCallback(
    async (selection: ModelSelection): Promise<boolean> => {
      const primaryRuntimeId = $activeSessionId.get()
      const liveSessionId = 'sessionId' in selection ? (selection.sessionId ?? null) : primaryRuntimeId
      const touchesPrimary = !liveSessionId || liveSessionId === primaryRuntimeId

      const prevModel = touchesPrimary ? $currentModel.get() : ($sessionStates.get()[liveSessionId!]?.model ?? '')

      const prevProvider = touchesPrimary
        ? $currentProvider.get()
        : ($sessionStates.get()[liveSessionId!]?.provider ?? '')

      const prevSource = getCurrentModelSource()
      const liveGatewayProfile = $activeGatewayProfile.get()

      if (touchesPrimary) {
        setCurrentModel(selection.model)
        setCurrentProvider(selection.provider)
        markComposerSelectionManual()
      } else if (liveSessionId) {
        // Optimistic tile paint — session.info will confirm; rollback on error.
        sessionTileDelegate()?.updateSession(liveSessionId, state => ({
          ...state,
          model: selection.model,
          provider: selection.provider
        }))
      }

      updateModelOptionsCache(
        liveSessionId,
        selection.provider,
        selection.model,
        touchesPrimary && !liveSessionId,
        liveGatewayProfile
      )

      // No live session yet: the pick is pure UI state. session.create reads
      // $currentModel/$currentProvider and applies it as that session's override.
      if (!liveSessionId) {
        return true
      }

      try {
        const result = await requestGateway<{ deferred?: boolean }>('config.set', {
          session_id: liveSessionId,
          key: 'model',
          value: `${selection.model} --provider ${selection.provider} --session`
        })

        // A pick made DURING a turn is queued by the gateway and applied at the
        // next turn start (`deferred`). Re-fetching now would answer with the
        // model still running and repaint the old name over the user's choice —
        // the switch publishes session.info when it lands, and that is what
        // re-syncs every surface.
        if (!result?.deferred) {
          void queryClient.invalidateQueries({ queryKey: modelOptionsQueryKey(liveGatewayProfile, liveSessionId) })
        }

        return true
      } catch (err) {
        // An OLDER gateway refuses a mid-turn switch outright (4009) instead of
        // deferring it. Don't punish the user for a backend they haven't
        // updated: keep the pick painted as the composer's selection, which is
        // what the NEXT turn runs anyway. Current gateways never take this
        // path — they answer `deferred`.
        if (isBusySessionModelSwitch(err)) {
          return true
        }

        if (touchesPrimary) {
          setCurrentModel(prevModel)
          setCurrentProvider(prevProvider)
          setCurrentModelSource(prevSource)
        } else if (liveSessionId) {
          sessionTileDelegate()?.updateSession(liveSessionId, state => ({
            ...state,
            model: prevModel,
            provider: prevProvider
          }))
        }

        updateModelOptionsCache(
          liveSessionId,
          prevProvider,
          prevModel,
          touchesPrimary && !liveSessionId,
          liveGatewayProfile
        )
        notifyError(err, copy.modelSwitchFailed)

        return false
      }
    },
    [copy.modelSwitchFailed, queryClient, requestGateway, updateModelOptionsCache]
  )

  /**
   * Reconcile the NEXT chat's model (no live session yet) before it is created.
   *
   * `session.create` ships the composer's `$currentModel`, so fixing only the
   * saved default is not enough: a composer seeded before the fix would still
   * create the session on the revoked model. Two cases:
   *
   * - default-derived composer: the send gate (main process) has just checked
   *   the saved default against the grant list — rewriting it if it was revoked
   *   — so its answer is the truth. Align the composer with it and, when the
   *   default was actually replaced, say so once.
   * - manual pick: compare against the cached catalog, like `keepManualPick`.
   *   Only a catalog that is present and non-empty can revoke (see
   *   `manualPickRemoved`), so an offline picker never yanks a pick.
   */
  const reconcileDraftModel = useCallback(
    async (gate?: null | ProductModelSendGate): Promise<boolean> => {
      const provider = $currentProvider.get()
      const model = $currentModel.get()

      if (getCurrentModelSource() === 'manual') {
        const cached = readCachedModelOptions(queryClient, $activeGatewayProfile.get(), null)
        const replacement = replacementModelFor(cached?.providers, provider, model, readyModel(gate))

        if (!replacement) {
          return true
        }

        const switched = await selectModel({ model: replacement, provider })

        if (switched) {
          notify({
            id: 'model-auto-switched-draft',
            durationMs: 0,
            kind: 'warning',
            message: copy.modelAutoSwitched(model, replacement)
          })
        }

        return switched
      }

      if (gate?.status !== 'ready' || !gate.model) {
        return true
      }

      if (gate.model !== model) {
        setCurrentModel(gate.model)
        setCurrentModelSource('default')
        updateModelOptionsCache(null, provider, gate.model, false)
      }

      if (gate.replacedFrom) {
        notify({
          id: 'default-model-auto-switched',
          durationMs: 0,
          kind: 'warning',
          message: copy.defaultModelAutoSwitched(gate.replacedFrom, gate.model)
        })
      }

      return true
    },
    [copy, queryClient, selectModel, updateModelOptionsCache]
  )

  /**
   * Reconcile a LIVE session's model against the gateway's current catalog.
   *
   * `refreshCurrentModel` bails out the moment a session exists — correct for
   * what it does (seed the composer from the profile default; a live session
   * owns its own footer), but it meant the catalog was only ever compared
   * against a pick on the new-chat path. An administrator who revoked a model
   * therefore kept it offered inside every conversation that was already open,
   * and the user only found out at send time, as a raw "model disabled" from
   * the gateway. Per-user model authorization is the core of this product; a
   * revocation the client argues with for the life of a session is not one.
   *
   * Deliberately asks the gateway instead of trusting the cache: the whole
   * failure mode being fixed is a cache nobody refreshed. The catalog endpoint
   * is already TTL'd to 60s server-side for exactly this reason.
   *
   * Returns whether sending may proceed. A catalog we could not fetch returns
   * `true` and changes nothing — treating a network blip as "your model was
   * revoked" would yank people off their chosen model while offline, which is
   * worse than letting one send fail with the gateway's own error.
   */
  const reconcileSessionModel = useCallback(
    async (sessionId?: null | string, gate?: null | ProductModelSendGate): Promise<boolean> => {
      const primaryRuntimeId = $activeSessionId.get()
      const liveSessionId = sessionId ?? primaryRuntimeId

      if (!liveSessionId) {
        return reconcileDraftModel(gate)
      }

      const touchesPrimary = liveSessionId === primaryRuntimeId
      const tileState = $sessionStates.get()[liveSessionId]
      const provider = touchesPrimary ? $currentProvider.get() : (tileState?.provider ?? '')
      const model = touchesPrimary ? $currentModel.get() : (tileState?.model ?? '')

      if (!provider || !model) {
        return true
      }

      const now = Date.now()

      if (now - (reconciledAtRef.current[liveSessionId] ?? 0) < RECONCILE_MIN_INTERVAL_MS) {
        return true
      }

      reconciledAtRef.current[liveSessionId] = now

      const profile = $activeGatewayProfile.get()
      let options: ModelOptionsResponse

      try {
        options = await requestGateway<ModelOptionsResponse>('model.options', {
          explicit_only: true,
          session_id: liveSessionId
        })
      } catch {
        return true
      }

      queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile, liveSessionId), options)

      // An empty provider list is NOT a revocation — `manualPickRemoved`
      // treats it as unconfigured / awaiting re-auth on purpose, and
      // `replacementModelFor` inherits that. The genuinely-no-models case is
      // already the send gate's own job (`no-grants`), so there is nothing to
      // do here beyond leaving the pick alone.
      const replacement = replacementModelFor(options?.providers, provider, model, readyModel(gate))

      if (!replacement) {
        return true
      }

      const switched = await selectModel({ model: replacement, provider, sessionId: liveSessionId })

      if (switched) {
        notify({
          // Session-scoped id: two open conversations can each have been moved
          // off a different model, and collapsing that into one banner would
          // name the wrong conversation.
          id: `model-auto-switched-${liveSessionId}`,
          durationMs: 0,
          kind: 'warning',
          message: copy.modelAutoSwitched(model, replacement)
        })
      }

      return switched
    },
    [copy, queryClient, reconcileDraftModel, requestGateway, selectModel]
  )

  return { applySavedMainModel, reconcileSessionModel, refreshCurrentModel, selectModel }
}
