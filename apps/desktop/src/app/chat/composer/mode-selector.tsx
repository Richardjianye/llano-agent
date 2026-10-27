import { useStore } from '@nanostores/react'

import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { useStoreSelector } from '@/lib/use-session-slice'
import { cn } from '@/lib/utils'
import {
  $draftConversationMode,
  $modeDraftAction,
  normalizeConversationMode,
  setDraftConversationMode
} from '@/store/conversation-mode'
import { $selectedStoredSessionId, $sessions, sessionMatchesStoredId } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { useComposerScope } from './scope'

interface ConversationModeSelectorProps {
  sessionId?: string | null
  disabled: boolean
  /** Compact surfaces show the frozen mode without suggesting a switch. */
  readOnly?: boolean
}

export function ConversationModeSelector({ sessionId, disabled, readOnly = false }: ConversationModeSelectorProps) {
  const { t } = useI18n()
  const copy = t.composer
  const draft = useStore($draftConversationMode)
  const action = useStore($modeDraftAction)
  const scope = useComposerScope()
  const selectedId = useStore($selectedStoredSessionId)
  const targetId = sessionId ?? (scope.target === 'main' ? selectedId : null)
  const runtimeMode = useStoreSelector($sessionStates, states => (sessionId ? states[sessionId]?.mode : undefined))

  const storedId = useStoreSelector($sessionStates, states =>
    sessionId ? states[sessionId]?.storedSessionId : undefined
  )

  const storedMode = useStoreSelector(
    $sessions,
    rows => rows.find(row => sessionMatchesStoredId(row, storedId ?? targetId ?? ''))?.mode
  )

  const mode = targetId ? normalizeConversationMode(runtimeMode ?? storedMode) : draft

  if (readOnly || scope.target !== 'main') {
    return (
      <span className="shrink-0 text-xs text-(--ui-text-tertiary)">
        {mode === 'chat' ? copy.chatMode : copy.workMode}
      </span>
    )
  }

  const options = [
    { id: 'chat' as const, label: copy.chatMode },
    { id: 'work' as const, label: copy.workMode }
  ]

  return (
    <div
      className="grid h-9 w-52 max-w-full grid-cols-2 rounded-full bg-(--ui-bg-tertiary) p-0.5"
      data-conversation-mode-selector=""
    >
      {options.map(option => {
        const active = mode === option.id
        const linkedDraft = Boolean(targetId) && !active
        const hint = targetId ? copy.modeLockedHint : option.id === 'chat' ? copy.chatModeHint : copy.workModeHint

        return (
          <Tip key={option.id} label={hint}>
            <button
              aria-label={linkedDraft ? (option.id === 'work' ? copy.newWorkMode : copy.newChatMode) : option.label}
              aria-pressed={active}
              className={cn(
                'flex min-w-0 cursor-pointer items-center justify-center rounded-full text-sm font-medium transition-colors disabled:cursor-default disabled:opacity-50',
                active ? 'bg-background text-foreground shadow-sm' : 'text-(--ui-text-secondary) hover:text-foreground'
              )}
              disabled={disabled || (linkedDraft && !action)}
              onClick={() => {
                if (active) {
                  return
                }

                if (targetId) {
                  action?.({ mode: option.id, relatedSessionId: storedId ?? targetId })
                } else {
                  setDraftConversationMode(option.id)
                }
              }}
              type="button"
            >
              {option.label}
            </button>
          </Tip>
        )
      })}
    </div>
  )
}
