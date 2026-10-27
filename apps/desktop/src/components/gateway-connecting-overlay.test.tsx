import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { $gatewaySwitching } from '@/store/gateway-switch'
import { $desktopOnboarding } from '@/store/onboarding'
import { $productAuthenticated } from '@/store/product-auth'
import { setGatewayState } from '@/store/session'

import { BootFailureOverlay } from './boot-failure-overlay'
import { GatewayConnectingOverlay } from './gateway-connecting-overlay'

// Repro for the "remote gateway → stuck on CONNECTING, no way to settings"
// report. The connecting overlay (full-screen, pointer-events on) used
// to be shown whenever `gatewayState !== 'open' && !boot.error`. The ONLY escape
// hatch — BootFailureOverlay, which has "Use local gateway" / "Sign in" /
// "Retry" — only renders when `boot.error` is set.
//
// useGatewayBoot only calls failDesktopBoot() (which sets boot.error) when the
// INITIAL boot() throws. After the first successful connect (bootCompleted),
// any later socket drop goes through scheduleReconnect(), which loops FOREVER
// against the dead remote. So gatewayState sits at 'closed'/'error' with
// boot.error null. The fix keeps the initial-boot overlay out of post-boot
// reconnects, leaving chat/settings usable while the reconnect loop runs.

function resetStores() {
  setGatewayState('idle')
  $gatewaySwitching.set(false)
  $productAuthenticated.set(true)
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: 'ready',
    phase: 'renderer.ready',
    progress: 100,
    running: false,
    timestamp: Date.now(),
    visible: false
  })
  $desktopOnboarding.set({
    configured: true,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  })
}

beforeEach(resetStores)
afterEach(cleanup)

// The connecting overlay renders "CONN" + a scrambled tail inside one
// uppercase span; match that node specifically so the recovery overlay's
// "Lost connection…" copy doesn't read as a false positive.
const isConnectingShown = () =>
  screen.queryAllByText((_, el) => /^CONN[/\\|\-_=+<>~:*A-Z]*$/.test(el?.textContent?.trim() ?? '')).length > 0

const isRecoveryShown = () =>
  Boolean(screen.queryByText(/use local gateway/i) || screen.queryByText(/retry/i) || screen.queryByText(/sign in/i))

describe('connecting overlay vs recovery surface', () => {
  it('shows screenshot-safe boot stage, determinate progress, and first-launch runtime guidance', () => {
    $desktopBoot.set({
      ...$desktopBoot.get(),
      message: '正在下载 Python：42%',
      phase: 'runtime.windows.prepare',
      progress: 51,
      running: true,
      visible: true
    })

    render(<GatewayConnectingOverlay />)

    expect(screen.getByText('Preparing the local runtime')).toBeTruthy()
    expect(screen.queryByText('正在下载 Python：42%')).toBeNull()
    expect(screen.getByText('51%')).toBeTruthy()
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('51')
    expect(screen.getByText(/^Please wait a moment$/)).toBeTruthy()
  })

  it('keeps the first-launch guidance up across check and prepare instead of flashing it', () => {
    // The manager alternates check -> download -> extract -> check for each of
    // the nine components. Tying the guidance to one of those phases made a
    // paragraph blink on and off for minutes, which reads as a glitch.
    $desktopBoot.set({
      ...$desktopBoot.get(),
      message: '正在检查 Python…',
      phase: 'runtime.windows.check',
      progress: 44,
      running: true,
      visible: true
    })

    render(<GatewayConnectingOverlay />)

    expect(screen.getByText('Preparing the local runtime')).toBeTruthy()
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('44')
    expect(screen.getByText(/^Please wait a moment$/)).toBeTruthy()
  })

  it('names the component and its own percentage, so a stuck first launch is a useful screenshot', () => {
    // Before: five minutes of one unchanging "Preparing the local runtime".
    // Support could not tell a slow browser-engine download from a hang.
    $desktopBoot.set({
      ...$desktopBoot.get(),
      message: '正在下载 浏览器引擎（192.0.2.130）…',
      phase: 'runtime.windows.prepare',
      progress: 63,
      running: true,
      step: { index: 8, total: 9, label: '浏览器引擎', kind: 'download', percent: 45 },
      visible: true
    })

    render(<GatewayConnectingOverlay />)

    expect(screen.getByText('8/9 浏览器引擎 · downloading 45%')).toBeTruthy()
    // The mirror host in the raw message still never reaches the screen.
    expect(screen.queryByText(/192\.168\./)).toBeNull()
  })

  it('shows the non-download stages without a bogus percentage', () => {
    $desktopBoot.set({
      ...$desktopBoot.get(),
      message: '正在准备 Python…',
      phase: 'runtime.windows.prepare',
      progress: 40,
      running: true,
      step: { index: 2, total: 9, label: 'Python', kind: 'extract' },
      visible: true
    })

    render(<GatewayConnectingOverlay />)

    expect(screen.getByText('2/9 Python · installing')).toBeTruthy()
  })

  it('drops the component line on phases that do not carry one', () => {
    $desktopBoot.set({
      ...$desktopBoot.get(),
      message: 'Starting Hermes backend',
      phase: 'backend.spawn',
      progress: 84,
      running: true,
      step: null,
      visible: true
    })

    render(<GatewayConnectingOverlay />)

    expect(screen.queryByText(/\d\/9/)).toBeNull()
  })

  it('never renders diagnostic paths or first-launch guidance for an external runtime', () => {
    const diagnostic = 'Using /Users/developer/private/venv/python'
    $desktopBoot.set({
      ...$desktopBoot.get(),
      message: diagnostic,
      phase: 'runtime.external',
      progress: 32,
      running: true,
      visible: true
    })

    render(<GatewayConnectingOverlay />)

    expect(screen.getByText('Preparing the local runtime')).toBeTruthy()
    expect(screen.queryByText(diagnostic)).toBeNull()
    expect(screen.queryByText(/^Please wait a moment$/)).toBeNull()
  })

  it('does not cover the branded login gate while signed out', () => {
    $productAuthenticated.set(false)
    $desktopBoot.set({ ...$desktopBoot.get(), progress: 20, running: true, visible: true })

    render(<GatewayConnectingOverlay />)

    expect(isConnectingShown()).toBe(false)
  })

  it('hard initial-boot failure surfaces the recovery overlay (the working path)', async () => {
    // failDesktopBoot() ran: error set, gateway never opened.
    $desktopBoot.set({
      ...$desktopBoot.get(),
      error: 'Hermes backend did not become ready',
      running: false,
      visible: true
    })
    setGatewayState('error')

    await act(async () => {
      render(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })

    expect(isRecoveryShown()).toBe(true)
    // Connecting overlay bows out when boot.error is set.
    expect(isConnectingShown()).toBe(false)
  })

  it('post-boot socket drops do not re-cover the app with the initial CONNECTING overlay', async () => {
    // 1. Initial boot succeeded: gateway opened, boot completed (no error).
    setGatewayState('open')

    let rerender!: (ui: React.ReactElement) => void
    await act(async () => {
      const result = render(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )

      rerender = result.rerender
    })

    expect(isConnectingShown()).toBe(false)

    // 2. The remote VPS socket drops (sleep/wake, remote restart, network).
    //    bootCompleted is true, so useGatewayBoot routes this through
    //    scheduleReconnect() — boot.error stays NULL.
    await act(async () => {
      setGatewayState('closed')
      rerender!(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })

    // The initial-boot connecting overlay stays out of the way, so settings and
    // the composer remain reachable during the reconnect loop.
    expect(isConnectingShown()).toBe(false)
    expect(isRecoveryShown()).toBe(false)

    // 3. Reconnect loops against the dead remote: gatewayState bounces closed
    //    → error → closed. Until the escalation path sets boot.error, the app
    //    remains usable instead of modal-blocked.
    await act(async () => {
      setGatewayState('error')
      rerender!(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })
    expect($desktopBoot.get().error).toBeNull()
    expect(isConnectingShown()).toBe(false)
    expect(isRecoveryShown()).toBe(false)
  })

  it('soft gateway switch keeps the shell — no fullscreen CONNECTING', async () => {
    setGatewayState('open')

    const { rerender } = render(
      <>
        <GatewayConnectingOverlay />
        <BootFailureOverlay />
      </>
    )

    await act(async () => {
      $gatewaySwitching.set(true)
      $desktopBoot.set({
        ...$desktopBoot.get(),
        running: true,
        visible: true,
        progress: 4,
        error: null
      })
      setGatewayState('closed')
      rerender(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })

    expect(isConnectingShown()).toBe(false)
    expect(isRecoveryShown()).toBe(false)
  })

  it('FIX: once the prolonged reconnect raises a recoverable boot error, the recovery overlay takes over', async () => {
    // Mirrors what useGatewayBoot.scheduleReconnect() now does after ~45s of
    // failed post-boot reconnects: it calls failDesktopBoot(), flipping the UI
    // from the dead-end CONNECTING overlay to the recovery surface.
    setGatewayState('error')
    $desktopBoot.set({
      ...$desktopBoot.get(),
      error: 'Lost connection to the Hermes gateway and could not reconnect.',
      running: false,
      visible: true
    })

    await act(async () => {
      render(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })

    // Company recovery is now reachable; the connecting overlay bows out.
    expect(isRecoveryShown()).toBe(true)
    expect(screen.getByRole('button', { name: /restart backend/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /gateway settings/i })).toBeNull()
    expect(isConnectingShown()).toBe(false)
  })
})
