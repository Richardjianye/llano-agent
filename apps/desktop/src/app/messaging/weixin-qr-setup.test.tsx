// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { WeixinOnboardingState, WeixinOnboardingStatus } from '@/types/hermes'

const startWeixinOnboarding = vi.fn()
const getWeixinOnboarding = vi.fn()
const applyWeixinOnboarding = vi.fn()
const cancelWeixinOnboarding = vi.fn()

vi.mock('@/hermes', () => ({
  applyWeixinOnboarding: (id: string) => applyWeixinOnboarding(id),
  cancelWeixinOnboarding: (id: string) => cancelWeixinOnboarding(id),
  getWeixinOnboarding: (id: string) => getWeixinOnboarding(id),
  startWeixinOnboarding: () => startWeixinOnboarding()
}))

function session(status: WeixinOnboardingStatus, patch: Partial<WeixinOnboardingState> = {}): WeixinOnboardingState {
  return {
    expires_at: '2026-10-19T12:10:00Z',
    pairing_id: 'pair-1',
    qr_payload: 'https://liteapp.weixin.qq.com/q/1',
    status,
    ...patch
  }
}

beforeEach(() => {
  cancelWeixinOnboarding.mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function renderSetup(props: { configured?: boolean; onConnected?: () => Promise<void> } = {}) {
  const { WeixinQrSetup } = await import('./weixin-qr-setup')
  const onConnected = props.onConnected ?? vi.fn().mockResolvedValue(undefined)
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <WeixinQrSetup configured={props.configured ?? false} onConnected={onConnected} pollIntervalMs={5} />
    )
  })

  return { onConnected, result: result! }
}

describe('WeixinQrSetup', () => {
  it('walks waiting → scanned → confirmed, applies, and reports the connected account', async () => {
    startWeixinOnboarding.mockResolvedValue(session('waiting'))
    let scanned = false
    getWeixinOnboarding.mockImplementation(async () => {
      if (!scanned) {
        scanned = true

        return session('scanned')
      }

      return session('confirmed', { account_id: 'bot-123@im.bot' })
    })
    applyWeixinOnboarding.mockResolvedValue({
      account_id: 'bot-123@im.bot',
      needs_restart: false,
      ok: true,
      platform: 'weixin',
      restart_started: true
    })

    const { onConnected } = await renderSetup()
    await act(async () => {
      fireEvent.click(screen.getByText('Show QR code'))
    })

    expect(await screen.findByAltText('WeChat login QR code')).toBeTruthy()
    await waitFor(() => expect(applyWeixinOnboarding).toHaveBeenCalledWith('pair-1'))
    expect(await screen.findByText('WeChat connected (bot-123@im.bot)')).toBeTruthy()
    expect(onConnected).toHaveBeenCalledTimes(1)
    // A finished session is applied, not cancelled.
    expect(cancelWeixinOnboarding).not.toHaveBeenCalled()
  })

  it('shows the scanned prompt while waiting for phone confirmation', async () => {
    startWeixinOnboarding.mockResolvedValue(session('waiting'))
    getWeixinOnboarding.mockResolvedValue(session('scanned'))

    await renderSetup()
    await act(async () => {
      fireEvent.click(screen.getByText('Show QR code'))
    })

    expect(await screen.findByText('Scanned. Confirm the login in WeChat on your phone.')).toBeTruthy()
    expect(applyWeixinOnboarding).not.toHaveBeenCalled()
  })

  it('offers a fresh QR code after expiry', async () => {
    startWeixinOnboarding
      .mockResolvedValueOnce(session('waiting'))
      .mockResolvedValueOnce(
        session('waiting', { pairing_id: 'pair-2', qr_payload: 'https://liteapp.weixin.qq.com/q/2' })
      )
    getWeixinOnboarding.mockImplementation(async (id: string) =>
      id === 'pair-1' ? session('expired') : session('waiting', { pairing_id: 'pair-2' })
    )

    await renderSetup()
    await act(async () => {
      fireEvent.click(screen.getByText('Show QR code'))
    })

    const retry = await screen.findByText('Get a new QR code')
    expect(screen.getByText('The QR code expired.')).toBeTruthy()
    await act(async () => {
      fireEvent.click(retry)
    })

    await waitFor(() => expect(startWeixinOnboarding).toHaveBeenCalledTimes(2))
    expect(await screen.findByAltText('WeChat login QR code')).toBeTruthy()
  })

  it('reports a failed start without leaving a spinner', async () => {
    startWeixinOnboarding.mockRejectedValue(new Error('502'))

    await renderSetup()
    await act(async () => {
      fireEvent.click(screen.getByText('Show QR code'))
    })

    expect(await screen.findByText('WeChat setup failed.')).toBeTruthy()
    expect(screen.getByText('Get a new QR code')).toBeTruthy()
  })

  it('cancels the backend session when the user cancels or leaves the page', async () => {
    startWeixinOnboarding.mockResolvedValue(session('waiting'))
    getWeixinOnboarding.mockResolvedValue(session('waiting'))

    const { result } = await renderSetup()
    await act(async () => {
      fireEvent.click(screen.getByText('Show QR code'))
    })
    await screen.findByAltText('WeChat login QR code')

    await act(async () => {
      fireEvent.click(screen.getByText('Cancel'))
    })
    expect(cancelWeixinOnboarding).toHaveBeenCalledWith('pair-1')
    expect(screen.getByText('Show QR code')).toBeTruthy()

    startWeixinOnboarding.mockResolvedValue(session('waiting', { pairing_id: 'pair-2' }))
    await act(async () => {
      fireEvent.click(screen.getByText('Show QR code'))
    })
    await screen.findByAltText('WeChat login QR code')
    result.unmount()
    expect(cancelWeixinOnboarding).toHaveBeenCalledWith('pair-2')
  })

  it('offers a rescan once WeChat is already configured', async () => {
    await renderSetup({ configured: true })

    expect(screen.getByText('Scan again')).toBeTruthy()
  })
})

describe('usesWeixinQrSetup', () => {
  it('replaces the CLI flow for Weixin only in branded builds', async () => {
    const { usesWeixinQrSetup } = await import('./weixin-qr-setup')

    expect(usesWeixinQrSetup('weixin', true)).toBe(true)
    expect(usesWeixinQrSetup('dingtalk', true)).toBe(false)
    expect(usesWeixinQrSetup('weixin', false)).toBe(false)
  })
})
