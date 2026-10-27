import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Loader } from '@/components/ui/loader'
import { cancelCollabTurn } from '@/hermes'
import { useI18n } from '@/i18n'
import { $collabProjects, $collabState, loadCollabTurns, sendCollab } from '@/store/collab'
import type { CollabPendingReply, CollabTurn } from '@/types/hermes'

export function replyRows(turns: CollabTurn[], pending: CollabPendingReply[], seq?: number) {
  const rows = new Map<string, { id?: number; owner_uid: string; label: string; status: string }>()

  for (const item of pending.filter(p => p.trigger_seq === seq)) {
    rows.set(`${item.owner_uid}:${item.agent_label}`, {
      owner_uid: item.owner_uid,
      label: item.agent_label,
      status: item.status
    })
  }

  for (const turn of [...turns]
    .filter(t =>
      seq === undefined ? t.trigger_reason !== 'mention' : t.trigger_reason === 'mention' && t.trigger_seq === seq
    )
    .sort((a, b) => a.id - b.id)) {
    rows.set(`${turn.owner_uid}:${turn.agent_label}`, {
      id: turn.id,
      owner_uid: turn.owner_uid,
      label: turn.agent_label || '',
      status: turn.status === 'completed' && turn.error_code === 'empty_reply' ? 'emptyReply' : turn.status
    })
  }

  return [...rows.values()].filter(row =>
    seq === undefined ? ['running', 'queued', 'waiting'].includes(row.status) : row.status !== 'completed'
  )
}

export function ReplyStatus({ code, seq }: { code: string; seq?: number }) {
  const { t } = useI18n()
  const c = t.collab.functional
  const state = useStore($collabState)[code]
  const archived = useStore($collabProjects).find(p => p.code === code)?.status === 'archived'
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const rows = replyRows(state?.turns || [], state?.pending || [], seq)
  const me = state?.members.find(m => m.is_me && m.member_kind !== 'agent')

  const statusLabel: Record<string, string> = {
    running: c.running,
    queued: c.queued,
    waiting: c.waiting,
    offline: c.offline,
    failed: c.failed,
    cancelled: c.cancelled,
    emptyReply: c.emptyReply
  }

  if (!rows.length && !error) {
    return null
  }

  return (
    <div
      aria-live="polite"
      className="space-y-2 rounded-lg border border-dashed border-(--ui-stroke-secondary) bg-primary/5 px-3 py-2"
    >
      <div className="text-xs text-(--ui-text-tertiary)">{c.replies}</div>
      {rows.map(row => (
        <div
          className="flex flex-wrap items-center gap-2 text-sm text-(--ui-text-secondary)"
          key={`${row.owner_uid}:${row.label}`}
        >
          {!state?.turnsStale && ['running', 'queued', 'waiting'].includes(row.status) ? <Loader /> : null}
          <span>
            {row.label} · {state?.turnsStale ? t.collab.offline : statusLabel[row.status] || row.status}
          </span>
          {row.status === 'running' && row.id && me?.user_uid === row.owner_uid ? (
            <Button
              disabled={busy}
              onClick={() => {
                setBusy(true)
                setError('')
                void cancelCollabTurn(code, row.id!)
                  .then(() => loadCollabTurns(code))
                  .catch(e => setError(String(e.message || e)))
                  .finally(() => setBusy(false))
              }}
              size="sm"
              variant="secondary"
            >
              {c.stopReply}
            </Button>
          ) : null}
          {seq !== undefined &&
          ['failed', 'cancelled', 'emptyReply'].includes(row.status) &&
          me &&
          me.role !== 'viewer' &&
          !state?.stoppedReason &&
          !archived ? (
            <Button
              disabled={busy}
              onClick={() => {
                setBusy(true)
                setError('')
                // A retry is a new explicit human request. Never reopen consumed turns.
                void sendCollab(code, `@${row.label} ${c.retryReply} #${seq}`)
                  .catch(e => setError(String(e.message || e)))
                  .finally(() => setBusy(false))
              }}
              size="sm"
              variant="secondary"
            >
              {c.retryReply}
            </Button>
          ) : null}
        </div>
      ))}
      {error ? (
        <p className="text-destructive" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  )
}
