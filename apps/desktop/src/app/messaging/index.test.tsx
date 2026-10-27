// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { notify, notifyError } from '@/store/notifications'
import type { MessagingPlatformInfo } from '@/types/hermes'

const controlMessagingGateway = vi.fn()
const getMessagingPlatforms = vi.fn()
const updateMessagingPlatform = vi.fn()
const getPairing = vi.fn()
const approvePairing = vi.fn()
const dismissPairing = vi.fn()
const revokePairing = vi.fn()
const openExternalLink = vi.fn()

vi.mock('@/hermes', () => ({
  approvePairing: (platformId: string, requestId: string) => approvePairing(platformId, requestId),
  controlMessagingGateway: (action: string) => controlMessagingGateway(action),
  dismissPairing: (platformId: string, requestId: string) => dismissPairing(platformId, requestId),
  getMessagingPlatforms: () => getMessagingPlatforms(),
  getPairing: () => getPairing(),
  revokePairing: (platformId: string, userId: string) => revokePairing(platformId, userId),
  updateMessagingPlatform: (id: string, body: unknown) => updateMessagingPlatform(id, body)
}))

vi.mock('@/lib/external-link', () => ({
  openExternalLink: (href: string) => openExternalLink(href)
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/system-actions', () => ({
  runGatewayRestart: vi.fn()
}))

function platform(patch: Partial<MessagingPlatformInfo> = {}): MessagingPlatformInfo {
  return {
    configured: false,
    description: 'A platform.',
    docs_url: '',
    enabled: false,
    env_vars: [],
    gateway_running: true,
    id: 'teams',
    name: 'Microsoft Teams',
    state: 'disabled',
    ...patch
  }
}

beforeEach(() => {
  controlMessagingGateway.mockResolvedValue({ ok: true, state: 'starting', gateway_running: false })
  updateMessagingPlatform.mockResolvedValue({ ok: true, platform: 'teams' })
  getPairing.mockResolvedValue({ approved: [], pending: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function renderMessaging() {
  const { MessagingView } = await import('./index')
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <MemoryRouter>
        <MessagingView />
      </MemoryRouter>
    )
  })

  return result!
}

describe('MessagingView setup-guide link', () => {
  it('hides the setup-guide button for a plugin platform with no docs URL', async () => {
    // Teams (and other plugin platforms) ship an empty docs_url. Rendering an
    // anchor with href="" let Electron resolve it to the app's own packaged
    // index.html and fail with an OS "file not found" dialog. The button must
    // simply not appear when there is no guide to open.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ docs_url: '' })] })

    await renderMessaging()

    expect((await screen.findAllByText('Microsoft Teams')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Open setup guide')).toBeNull()
  })

  it('opens a real docs URL through the validated external opener', async () => {
    const docsUrl = 'https://hermes-agent.nousresearch.com/docs/user-guide/messaging/teams'
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ docs_url: docsUrl })] })

    await renderMessaging()

    const link = await screen.findByText('Open setup guide')
    await act(async () => {
      fireEvent.click(link)
    })

    await waitFor(() => expect(openExternalLink).toHaveBeenCalledWith(docsUrl))
  })
})

describe('MessagingView pairing', () => {
  const pendingUser = {
    age_minutes: 3,
    platform: 'teams',
    request_id: 'a1b2c3d4e5f60718',
    user_id: '7712345',
    user_name: 'Bee'
  }

  it('approves the listed request by its request id, never by a code', async () => {
    // The whole point of the request-id grant path: the UI can only ever send
    // the server-side row id, because the one-time code is never returned by
    // the API. Posting anything derived from the code could not be approved.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [pendingUser] })
    approvePairing.mockResolvedValue({ ok: true, user: { user_id: '7712345', user_name: 'Bee' } })

    await renderMessaging()

    const approve = await screen.findByRole('button', { name: 'Approve' })
    await act(async () => {
      fireEvent.click(approve)
    })

    await waitFor(() => expect(approvePairing).toHaveBeenCalledWith('teams', 'a1b2c3d4e5f60718'))
  })

  it('restores the pending row when approval fails', async () => {
    // Optimistic removal must not silently swallow the request: a failed
    // approve has to leave the operator something to retry.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [pendingUser] })
    approvePairing.mockRejectedValue(new Error('500 boom'))

    await renderMessaging()

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    })

    expect(await screen.findByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(screen.getByText('Bee')).toBeTruthy()
  })

  it('ignores a request by its request id without approving it', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValueOnce({ approved: [], pending: [pendingUser] })
    getPairing.mockResolvedValue({ approved: [], pending: [] })
    dismissPairing.mockResolvedValue({ ok: true, user: { user_id: '7712345', user_name: 'Bee' } })

    await renderMessaging()

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Ignore' }))
    })

    await waitFor(() => expect(dismissPairing).toHaveBeenCalledWith('teams', 'a1b2c3d4e5f60718'))
    expect(approvePairing).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull())
  })

  it('restores the pending row when ignoring fails', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [pendingUser] })
    dismissPairing.mockRejectedValue(new Error('500 boom'))

    await renderMessaging()

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Ignore' }))
    })

    expect(await screen.findByRole('button', { name: 'Ignore' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(notifyError).toHaveBeenCalled()
  })

  it('shows no pairing affordance when nobody is waiting', async () => {
    // Approvals are rare; an always-present empty state would be permanent
    // chrome on a page that is otherwise about credentials.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [] })

    await renderMessaging()

    expect((await screen.findAllByText('Microsoft Teams')).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.queryByText(/Pending requests/)).toBeNull()
  })

  it('still renders platforms when the pairing endpoint fails', async () => {
    // An older backend without the endpoint must not blank the page.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockRejectedValue(new Error('404 not found'))

    await renderMessaging()

    expect((await screen.findAllByText('Microsoft Teams')).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('refetches pending rows on pairing.changed, not on platforms.changed', async () => {
    // The two signals are not interchangeable: platforms.changed tracks
    // connect/disconnect health via gateway_state.json, which a new pairing
    // request never moves. Riding it would leave someone invisible in the
    // pending list until an unrelated reconnect happened to fire.
    const { $changeEventsAvailable, $pairingChangeTick, $platformsChangeTick } = await import('@/store/live-sync')

    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [] })

    await renderMessaging()
    await act(async () => {
      $changeEventsAvailable.set(true)
    })
    getPairing.mockClear()

    // Someone DMs the bot: the store moves, the watcher ticks pairing.changed.
    getPairing.mockResolvedValue({ approved: [], pending: [pendingUser] })
    await act(async () => {
      $pairingChangeTick.set($pairingChangeTick.get() + 1)
    })

    await waitFor(() => expect(getPairing).toHaveBeenCalled())
    expect(await screen.findByRole('button', { name: 'Approve' })).toBeTruthy()

    // A platform health tick alone must not be what fetches pairing.
    getPairing.mockClear()
    await act(async () => {
      $platformsChangeTick.set($platformsChangeTick.get() + 1)
    })
    expect(getPairing).not.toHaveBeenCalled()
  })
})

describe('MessagingView gateway controls', () => {
  it('separates configured credentials from disabled adapter and stopped gateway', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ configured: true, gateway_running: false })] })
    await renderMessaging()
    expect(await screen.findByText('Credentials set')).toBeTruthy()
    expect(screen.getByText('Messaging gateway stopped')).toBeTruthy()
    expect(screen.queryByText('Needs setup')).toBeNull()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Start messaging gateway' }))
    })
    expect(controlMessagingGateway).toHaveBeenCalledWith('start')
    expect(getMessagingPlatforms.mock.calls.length).toBeGreaterThan(1)
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ title: 'Messaging gateway start requested' }))
  })

  it('keeps re-fetching after start until the spawned gateway reports running', async () => {
    // The start endpoint answers before the new process has written its pid
    // and state, so the refresh right after it still says "stopped". The page
    // must keep looking until the gateway settles instead of needing a reload.
    const stopped = { platforms: [platform({ configured: true, enabled: true, gateway_running: false })] }

    const running = {
      platforms: [platform({ configured: true, enabled: true, gateway_running: true, state: 'connected' })]
    }

    getMessagingPlatforms.mockResolvedValue(stopped)
    controlMessagingGateway.mockResolvedValueOnce({ ok: true, state: 'starting', gateway_running: false })
    await renderMessaging()
    getMessagingPlatforms.mockResolvedValueOnce(stopped).mockResolvedValue(running)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Start messaging gateway' }))
    })
    expect(controlMessagingGateway).toHaveBeenCalledWith('start')
    await waitFor(() => expect(screen.getByText('Messaging gateway running')).toBeTruthy(), { timeout: 3000 })
    expect(screen.getByRole('button', { name: 'Restart messaging gateway' })).toBeTruthy()
  })

  it('stops re-fetching once the gateway has settled', async () => {
    getMessagingPlatforms.mockResolvedValue({
      platforms: [platform({ configured: true, enabled: true, gateway_running: false })]
    })
    await renderMessaging()
    getMessagingPlatforms.mockResolvedValue({
      platforms: [platform({ configured: true, enabled: true, gateway_running: true, state: 'connected' })]
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Start messaging gateway' }))
    })
    const calls = getMessagingPlatforms.mock.calls.length
    await new Promise(resolve => setTimeout(resolve, 1200))
    expect(getMessagingPlatforms.mock.calls.length).toBe(calls)
  })

  it('restarts a running embedded messaging gateway through the dedicated API', async () => {
    getMessagingPlatforms.mockResolvedValue({
      platforms: [platform({ configured: true, enabled: true, state: 'connected' })]
    })
    await renderMessaging()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Restart messaging gateway' }))
    })
    expect(controlMessagingGateway).toHaveBeenCalledWith('restart')
    expect(screen.getByText('Messaging gateway running')).toBeTruthy()
  })

  it('reports stopped when restart applies the disabling of all adapters', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ configured: true, gateway_running: true })] })
    controlMessagingGateway.mockResolvedValueOnce({ ok: true, state: 'stopped', gateway_running: false })
    await renderMessaging()
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ configured: true, gateway_running: false })] })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Restart messaging gateway' }))
    })
    expect(controlMessagingGateway).toHaveBeenCalledWith('restart')
    expect(notify).toHaveBeenCalledWith({ kind: 'success', title: 'Messaging gateway stopped', message: '' })
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ title: 'Messaging gateway start requested' }))
    expect(screen.getByRole('button', { name: 'Start messaging gateway' })).toBeTruthy()
  })

  it('surfaces lifecycle failures and leaves the start action available for recovery', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ gateway_running: false })] })
    controlMessagingGateway.mockRejectedValueOnce(new Error('embedded spawn failed'))
    await renderMessaging()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Start messaging gateway' }))
    })
    expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Could not start / restart the messaging gateway.')
    expect(screen.getByRole('button', { name: 'Start messaging gateway' }).hasAttribute('disabled')).toBe(false)
    expect(notify).not.toHaveBeenCalled()
  })

  it('refreshes saved credential presence and clears the password input without rendering stored secrets', async () => {
    const field = {
      advanced: false,
      description: 'Client secret',
      is_password: true,
      is_set: false,
      key: 'DINGTALK_CLIENT_SECRET',
      prompt: 'Client secret',
      redacted_value: null,
      required: true,
      url: null
    }

    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ gateway_running: false, env_vars: [field] })] })
    await renderMessaging()
    const input = window.document.getElementById('messaging-field-DINGTALK_CLIENT_SECRET') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'temporary-test-secret' } })
    getMessagingPlatforms.mockResolvedValue({
      platforms: [
        platform({
          configured: true,
          gateway_running: false,
          env_vars: [{ ...field, is_set: true, redacted_value: 'must-not-render-secret' }]
        })
      ]
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    })
    expect(updateMessagingPlatform).toHaveBeenCalledWith('teams', {
      env: { DINGTALK_CLIENT_SECRET: 'temporary-test-secret' }
    })
    expect(screen.getByText('Credentials set')).toBeTruthy()
    expect(input.value).toBe('')
    expect(window.document.body.textContent).not.toContain('must-not-render-secret')
    expect(input.placeholder).not.toContain('must-not-render-secret')
  })

  it('enabling with the gateway stopped starts it right away', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ configured: true, gateway_running: false })] })
    await renderMessaging()
    getMessagingPlatforms.mockResolvedValue({
      platforms: [platform({ configured: true, enabled: true, gateway_running: false, state: 'gateway_stopped' })]
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('switch'))
    })
    expect(updateMessagingPlatform).toHaveBeenCalledWith('teams', { enabled: true })
    expect(screen.queryByText('Needs setup')).toBeNull()
    await waitFor(() => expect(controlMessagingGateway).toHaveBeenCalledWith('start'))
  })

  it('disabling while the gateway runs restarts it so the channel actually stops', async () => {
    getMessagingPlatforms.mockResolvedValue({
      platforms: [platform({ configured: true, enabled: true, gateway_running: true, state: 'connected' })]
    })
    controlMessagingGateway.mockResolvedValue({ ok: true, state: 'running', gateway_running: true })
    await renderMessaging()
    getMessagingPlatforms.mockResolvedValue({
      platforms: [platform({ configured: true, enabled: false, gateway_running: true, state: 'disabled' })]
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('switch'))
    })
    expect(updateMessagingPlatform).toHaveBeenCalledWith('teams', { enabled: false })
    await waitFor(() => expect(controlMessagingGateway).toHaveBeenCalledWith('restart'))
  })

  it('disabling with the gateway stopped needs no restart', async () => {
    getMessagingPlatforms.mockResolvedValue({
      platforms: [platform({ configured: true, enabled: true, gateway_running: false, state: 'gateway_stopped' })]
    })
    await renderMessaging()
    getMessagingPlatforms.mockResolvedValue({
      platforms: [platform({ configured: true, enabled: false, gateway_running: false, state: 'disabled' })]
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('switch'))
    })
    expect(updateMessagingPlatform).toHaveBeenCalledWith('teams', { enabled: false })
    expect(controlMessagingGateway).not.toHaveBeenCalled()
    expect(vi.mocked(notify).mock.calls[0][0].action).toBeUndefined()
  })
})

describe('MessagingView zh copy', () => {
  it('replaces the backend English card subtitle and field label under the zh locale', async () => {
    getMessagingPlatforms.mockResolvedValue({
      platforms: [
        platform({
          description: 'Connect Hermes to DingTalk groups (钉钉).',
          env_vars: [
            {
              advanced: false,
              description: 'DingTalk client ID (App key)',
              is_password: false,
              is_set: false,
              key: 'DINGTALK_CLIENT_ID',
              prompt: 'Client ID',
              redacted_value: null,
              required: true,
              url: null
            }
          ],
          id: 'dingtalk',
          name: 'DingTalk'
        })
      ]
    })
    const { I18nProvider } = await import('@/i18n')
    const { MessagingView } = await import('./index')
    await act(async () => {
      render(
        <I18nProvider configClient={null} initialLocale="zh">
          <MemoryRouter>
            <MessagingView />
          </MemoryRouter>
        </I18nProvider>
      )
    })

    expect(await screen.findByText(/接入钉钉私聊和群聊/)).toBeTruthy()
    expect(screen.queryByText('Connect Hermes to DingTalk groups (钉钉).')).toBeNull()
    expect(screen.getAllByText('Client ID（App key）').length).toBeGreaterThan(0)
  })
})
