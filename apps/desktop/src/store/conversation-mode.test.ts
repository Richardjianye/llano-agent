import { beforeEach, describe, expect, it } from 'vitest'

import { $activeGatewayProfile, $newChatProfile } from '@/store/profile'
import { $connection } from '@/store/session'

import {
  $draftConversationMode,
  $draftModes,
  $draftRelatedSessionId,
  normalizeConversationMode,
  setDraftConversationMode,
  setDraftRelatedSessionId
} from './conversation-mode'

describe('conversation mode scope', () => {
  beforeEach(() => {
    $draftModes.set({})
    $connection.set(null)
    $newChatProfile.set(null)
    $activeGatewayProfile.set('default')
    setDraftRelatedSessionId(null)
  })

  it('defaults legacy or unknown mode to work', () => {
    expect(normalizeConversationMode(undefined)).toBe('work')
    expect(normalizeConversationMode('invalid')).toBe('work')
    expect($draftConversationMode.get()).toBe('work')
  })

  it('isolates selections and relationship across profiles', () => {
    setDraftConversationMode('chat')
    setDraftRelatedSessionId('source-default')
    $activeGatewayProfile.set('other')
    expect($draftConversationMode.get()).toBe('work')
    expect($draftRelatedSessionId.get()).toBeNull()
    setDraftConversationMode('work')
    $activeGatewayProfile.set('default')
    expect($draftConversationMode.get()).toBe('chat')
    expect($draftRelatedSessionId.get()).toBe('source-default')
  })

  it('isolates two gateways even when profile names agree', () => {
    $connection.set({ mode: 'remote', baseUrl: 'https://one.example' } as never)
    setDraftConversationMode('chat')
    setDraftRelatedSessionId('remote-source')
    $connection.set({ mode: 'remote', baseUrl: 'https://two.example' } as never)
    expect($draftConversationMode.get()).toBe('work')
    expect($draftRelatedSessionId.get()).toBeNull()
  })
})
