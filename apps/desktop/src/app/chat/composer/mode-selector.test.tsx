import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $draftConversationMode, $draftModes, $modeDraftAction } from '@/store/conversation-mode'
import { $activeGatewayProfile, $newChatProfile } from '@/store/profile'
import { $connection, $selectedStoredSessionId, $sessions } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { ConversationModeSelector } from './mode-selector'
import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from './scope'

const show = (sessionId?: string) =>
  render(
    <I18nProvider configClient={null} initialLocale="en">
      <ConversationModeSelector disabled={false} sessionId={sessionId} />
    </I18nProvider>
  )

beforeEach(() => {
  $connection.set(null)
  $activeGatewayProfile.set('default')
  $newChatProfile.set(null)
  $draftModes.set({})
  $selectedStoredSessionId.set(null)
  $sessionStates.set({})
  $sessions.set([])
  $modeDraftAction.set(null)
})
afterEach(cleanup)

describe('mode selector lifecycle', () => {
  it('selects draft mode without creating a conversation or sending a prompt', () => {
    const create = vi.fn()
    $modeDraftAction.set(create)
    show()
    fireEvent.click(screen.getByRole('button', { name: 'Chat' }))
    expect($draftConversationMode.get()).toBe('chat')
    expect(create).not.toHaveBeenCalled()
  })

  it('starts another mode as a new associated draft and does not alter the existing runtime', () => {
    const create = vi.fn()
    $modeDraftAction.set(create)
    $sessionStates.set({ runtime: { mode: 'chat', storedSessionId: 'stored' } as never })
    const original = $sessionStates.get().runtime
    show('runtime')
    fireEvent.click(screen.getByRole('button', { name: 'Start a new work conversation' }))
    expect(create).toHaveBeenCalledWith({ mode: 'work', relatedSessionId: 'stored' })
    expect($sessionStates.get().runtime).toBe(original)
  })

  it('reads durable mode while historical conversation is still resuming', () => {
    const create = vi.fn()
    $modeDraftAction.set(create)
    $selectedStoredSessionId.set('stored')
    $sessions.set([{ id: 'stored', mode: 'chat' } as never])
    show()
    expect(screen.getByRole('button', { name: 'Chat' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'Start a new work conversation' }).textContent).toBe('Work')
    fireEvent.click(screen.getByRole('button', { name: 'Start a new work conversation' }))
    expect(create).toHaveBeenCalledWith({ mode: 'work', relatedSessionId: 'stored' })
  })

  it('keeps both segments visible and treats the selected frozen segment as inert', () => {
    const create = vi.fn()
    $modeDraftAction.set(create)
    $sessionStates.set({ runtime: { mode: 'work', storedSessionId: 'stored' } as never })
    show('runtime')
    fireEvent.click(screen.getByRole('button', { name: 'Work' }))
    expect(create).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Start a new chat conversation' }).textContent).toBe('Chat')
    expect($sessionStates.get().runtime.mode).toBe('work')
  })

  it('does not offer mode switching inside a split conversation', () => {
    const create = vi.fn()
    $modeDraftAction.set(create)
    $sessionStates.set({ runtime: { mode: 'chat', storedSessionId: 'stored' } as never })
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target: 'tile:stored' }}>
          <ConversationModeSelector disabled={false} sessionId="runtime" />
        </ComposerScopeProvider>
      </I18nProvider>
    )
    expect(screen.getByText('Chat')).toBeTruthy()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(create).not.toHaveBeenCalled()
  })
})
