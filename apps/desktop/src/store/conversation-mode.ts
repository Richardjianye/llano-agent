import { atom, computed } from 'nanostores'

import { readJson, writeJson } from '@/lib/storage'
import { $activeGatewayProfile, $newChatProfile, normalizeProfileKey } from '@/store/profile'
import { $connection } from '@/store/session'

export type ConversationMode = 'chat' | 'work'
export const normalizeConversationMode = (value: unknown): ConversationMode => (value === 'chat' ? 'chat' : 'work')
const STORAGE_KEY = 'hermes.desktop.conversationMode.byConnectionProfile'
export const $draftModes = atom<Record<string, ConversationMode>>(readJson(STORAGE_KEY) ?? {})
export const $modeScope = computed(
  [$connection, $newChatProfile, $activeGatewayProfile],
  (connection, selected, active) =>
    JSON.stringify([connection?.mode ?? 'local', connection?.baseUrl ?? '', normalizeProfileKey(selected ?? active)])
)
export const $draftConversationMode = computed([$draftModes, $modeScope], (modes, scope) =>
  normalizeConversationMode(modes[scope])
)

export function setDraftConversationMode(mode: ConversationMode): void {
  const next = { ...$draftModes.get(), [$modeScope.get()]: mode }
  $draftModes.set(next)
  writeJson(STORAGE_KEY, next)
}

export interface ModeDraftOptions {
  mode: ConversationMode
  relatedSessionId?: string | null
}
export const $modeDraftAction = atom<((options: ModeDraftOptions) => void) | null>(null)
const $relatedSessions = atom<Record<string, string | null>>({})
export const $draftRelatedSessionId = computed(
  [$relatedSessions, $modeScope],
  (related, scope) => related[scope] ?? null
)

export function setDraftRelatedSessionId(id: string | null): void {
  $relatedSessions.set({ ...$relatedSessions.get(), [$modeScope.get()]: id })
}
