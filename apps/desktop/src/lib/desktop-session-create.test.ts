import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const route = vi.hoisted(() => ({ ensure: vi.fn() }))
vi.mock('@/store/profile', () => ({
  $activeGatewayProfile: atom('default'),
  $newChatProfile: atom(null),
  ensureGatewayProfile: route.ensure,
  normalizeProfileKey: (name: string | null) => name || 'default'
}))

import { $draftModes, setDraftConversationMode, setDraftRelatedSessionId } from '@/store/conversation-mode'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection } from '@/store/session'

import { desktopSessionCreateParams } from './desktop-session-create'

describe('desktop session mode boundary', () => {
  beforeEach(() => {
    $connection.set(null)
    $activeGatewayProfile.set('default')
    $draftModes.set({})
    setDraftRelatedSessionId(null)
    route.ensure.mockReset()
  })

  it('freezes mode and association before async profile handshake', async () => {
    let release!: () => void
    route.ensure.mockImplementation(
      () =>
        new Promise<void>(resolve => {
          release = resolve
        })
    )
    setDraftConversationMode('chat')
    setDraftRelatedSessionId('parent-id')
    const result = desktopSessionCreateParams('', true)
    setDraftConversationMode('work')
    setDraftRelatedSessionId(null)
    release()
    expect(await result).toMatchObject({ source: 'desktop', mode: 'chat', related_session_id: 'parent-id' })
  })

  it('does not carry primary draft association to unrelated split sessions', async () => {
    setDraftRelatedSessionId('parent-id')
    const params = await desktopSessionCreateParams('')
    expect(params.mode).toBe('work')
    expect(params).not.toHaveProperty('related_session_id')
  })
})
