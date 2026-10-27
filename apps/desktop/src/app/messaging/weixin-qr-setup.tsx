import * as QRCode from 'qrcode'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  applyWeixinOnboarding,
  cancelWeixinOnboarding,
  getWeixinOnboarding,
  startWeixinOnboarding,
  type WeixinOnboardingState
} from '@/hermes'
import { useI18n } from '@/i18n'
import { IS_BRANDED_PRODUCT } from '@/lib/product-policy'
import { cn } from '@/lib/utils'

/** Branded builds hide the CLI, so Weixin's `hermes gateway setup` QR login
 *  moves into the messaging page. Stock keeps its CLI instructions. */
export const usesWeixinQrSetup = (platformId: string, branded: boolean = IS_BRANDED_PRODUCT): boolean =>
  branded && platformId === 'weixin'

type Phase =
  | { kind: 'applying' }
  | { kind: 'connected'; accountId: string; restartFailed: boolean }
  | { kind: 'failed'; expired: boolean }
  | { kind: 'idle' }
  | { kind: 'session'; session: WeixinOnboardingState }
  | { kind: 'starting' }

export interface WeixinQrSetupProps {
  configured: boolean
  onConnected: () => Promise<void> | void
  pollIntervalMs?: number
}

const sleep = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))

const qrDataUrl = async (payload: string) =>
  `data:image/svg+xml;charset=utf-8,${encodeURIComponent(
    await QRCode.toString(payload, { errorCorrectionLevel: 'M', margin: 1, type: 'svg' })
  )}`

export function WeixinQrSetup({ configured, onConnected, pollIntervalMs = 1500 }: WeixinQrSetupProps) {
  const { t } = useI18n()
  const m = t.messaging.weixinQr
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' })
  const [qrSrc, setQrSrc] = useState<null | string>(null)
  // Bumped by every start/cancel/unmount so a superseded poll loop or a late
  // response can never paint over newer intent.
  const runRef = useRef(0)
  const pairingRef = useRef<null | string>(null)

  const releasePairing = () => {
    const pairingId = pairingRef.current
    pairingRef.current = null

    if (pairingId) {
      void cancelWeixinOnboarding(pairingId).catch(() => undefined)
    }
  }

  // Leaving the page stops the backend's iLink poller too.
  useEffect(
    () => () => {
      runRef.current += 1
      releasePairing()
    },
    []
  )

  const payload = phase.kind === 'session' ? phase.session.qr_payload : null

  useEffect(() => {
    if (!payload) {
      setQrSrc(null)

      return
    }

    let cancelled = false

    qrDataUrl(payload)
      .then(src => void (cancelled || setQrSrc(src)))
      .catch(() => void (cancelled || setQrSrc(null)))

    return () => {
      cancelled = true
    }
  }, [payload])

  async function apply(run: number, pairingId: string) {
    setPhase({ kind: 'applying' })

    try {
      const result = await applyWeixinOnboarding(pairingId)
      pairingRef.current = null
      await onConnected()

      if (runRef.current === run) {
        setPhase({ accountId: result.account_id, kind: 'connected', restartFailed: !result.restart_started })
      }
    } catch {
      if (runRef.current === run) {
        setPhase({ expired: false, kind: 'failed' })
      }
    }
  }

  async function poll(run: number, pairingId: string) {
    while (runRef.current === run) {
      await sleep(pollIntervalMs)

      if (runRef.current !== run) {
        return
      }

      let next: WeixinOnboardingState

      try {
        next = await getWeixinOnboarding(pairingId)
      } catch {
        if (runRef.current === run) {
          pairingRef.current = null
          setPhase({ expired: false, kind: 'failed' })
        }

        return
      }

      if (runRef.current !== run) {
        return
      }

      if (next.status === 'confirmed') {
        await apply(run, pairingId)

        return
      }

      if (next.status === 'expired' || next.status === 'error' || next.status === 'cancelled') {
        pairingRef.current = null
        setPhase({ expired: next.status === 'expired', kind: 'failed' })

        return
      }

      setPhase({ kind: 'session', session: next })
    }
  }

  async function start() {
    const run = ++runRef.current
    releasePairing()
    setPhase({ kind: 'starting' })

    let session: WeixinOnboardingState

    try {
      session = await startWeixinOnboarding()
    } catch {
      if (runRef.current === run) {
        setPhase({ expired: false, kind: 'failed' })
      }

      return
    }

    if (runRef.current !== run) {
      void cancelWeixinOnboarding(session.pairing_id).catch(() => undefined)

      return
    }

    pairingRef.current = session.pairing_id
    setPhase({ kind: 'session', session })
    void poll(run, session.pairing_id)
  }

  function cancel() {
    runRef.current += 1
    releasePairing()
    setPhase({ kind: 'idle' })
  }

  return (
    <div className="mt-1 grid gap-3">
      <p className="text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {m.intro}
      </p>

      {phase.kind === 'idle' && (
        <div className="grid justify-items-start gap-2">
          {configured && <p className="text-xs leading-5 text-muted-foreground">{m.configuredHint}</p>}
          <Button onClick={() => void start()} size="sm" variant={configured ? 'secondary' : 'default'}>
            {configured ? m.rescan : m.start}
          </Button>
        </div>
      )}

      {(phase.kind === 'starting' || phase.kind === 'session') && (
        <div className="flex flex-wrap items-center gap-4">
          <div className="grid size-44 shrink-0 place-items-center rounded-lg border border-border bg-white p-2">
            {qrSrc ? (
              <img
                alt={m.qrAlt}
                className={cn(
                  'size-full transition-opacity',
                  phase.kind === 'session' && phase.session.status === 'scanned' && 'opacity-30'
                )}
                src={qrSrc}
              />
            ) : (
              <span className="text-xs text-neutral-500">{m.starting}</span>
            )}
          </div>
          <div className="grid min-w-0 flex-1 justify-items-start gap-2">
            <p aria-live="polite" className="text-sm leading-6" role="status">
              {phase.kind === 'starting' ? m.starting : phase.session.status === 'scanned' ? m.scanned : m.waiting}
            </p>
            <Button onClick={cancel} size="sm" variant="ghost">
              {m.cancel}
            </Button>
          </div>
        </div>
      )}

      {phase.kind === 'applying' && (
        <p aria-live="polite" className="text-sm leading-6" role="status">
          {m.applying}
        </p>
      )}

      {phase.kind === 'connected' && (
        <div aria-live="polite" className="grid gap-1" role="status">
          <p className="text-sm font-medium leading-6">{m.connected(phase.accountId)}</p>
          <p className="text-xs leading-5 text-muted-foreground">
            {phase.restartFailed ? m.restartFailed : m.connectedHint}
          </p>
        </div>
      )}

      {phase.kind === 'failed' && (
        <div className="grid justify-items-start gap-2">
          <p aria-live="polite" className="text-sm leading-6" role="status">
            {phase.expired ? m.expired : m.failed}
          </p>
          <Button onClick={() => void start()} size="sm" variant="secondary">
            {m.retry}
          </Button>
        </div>
      )}
    </div>
  )
}
