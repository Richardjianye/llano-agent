import { useState } from 'react'
import { useNavigate } from 'react-router'

import { openSession } from '@/app/open-session'
import { CompactMarkdown } from '@/components/chat/compact-markdown'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { acceptCollabTaskResult, getCollabTaskResult, submitCollabTaskResult } from '@/hermes'
import { useI18n } from '@/i18n'
import { $collabAccountEpoch, loadCollabHistory, refreshCollabTasks } from '@/store/collab'
import type { CollabTask } from '@/types/hermes'

import { latestTaskReply, taskBinding } from './task-execution'

export function TaskResult({
  code,
  task,
  mine,
  canAccept,
  readOnly = false
}: {
  code: string
  task: CollabTask
  mine: boolean
  canAccept: boolean
  readOnly?: boolean
}) {
  const { t } = useI18n()
  const c = t.collab.functional
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [review, setReview] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const binding = mine ? taskBinding(code, task.id) : undefined

  async function act(fn: () => Promise<unknown>, close = false) {
    setBusy(true)
    setError('')

    try {
      await fn()

      if (close) {
        setOpen(false)
      }

      await Promise.allSettled([refreshCollabTasks(code), loadCollabHistory(code)])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {binding ? (
        <Button onClick={() => openSession(binding.storedId, navigate, 'stack')} size="sm" variant="secondary">
          {c.viewPrivate}
        </Button>
      ) : null}
      {!readOnly && mine && task.status === 'active' ? (
        <Button onClick={() => setOpen(true)} size="sm" variant="secondary">
          {c.submitResult}
        </Button>
      ) : null}
      {task.result_message_id ? (
        <Button
          disabled={busy}
          onClick={() =>
            void act(async () => {
              const epoch = $collabAccountEpoch.get()
              const result = await getCollabTaskResult(code, task.id)

              if (epoch === $collabAccountEpoch.get()) {
                setReview(result.message.content)
              }
            })
          }
          size="sm"
          variant="secondary"
        >
          {c.reviewResult}
        </Button>
      ) : null}
      {!readOnly && canAccept && task.status === 'submitted' ? (
        <Button disabled={busy} onClick={() => void act(() => acceptCollabTaskResult(code, task.id))} size="sm">
          {c.acceptResult}
        </Button>
      ) : null}
      <Dialog
        onOpenChange={value => {
          if (!value) {
            setReview(null)
          }
        }}
        open={review !== null}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{c.reviewResult}</DialogTitle>
          </DialogHeader>
          <CompactMarkdown text={review || ''} />
        </DialogContent>
      </Dialog>
      <Dialog
        onOpenChange={value => {
          if (!busy) {
            setOpen(value)
          }
        }}
        open={open}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {task.title} · {c.result}
            </DialogTitle>
          </DialogHeader>
          <p className="text-sm">{c.resultHint}</p>
          {binding ? (
            <Button
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  const reply = await latestTaskReply(code, task.id)

                  if (!reply) {
                    throw new Error(c.noPrivateResult)
                  }

                  setText(reply)
                })
              }
              variant="secondary"
            >
              {c.loadResult}
            </Button>
          ) : null}
          <Textarea
            aria-label={c.result}
            className="min-h-48"
            disabled={busy}
            onChange={e => setText(e.target.value)}
            value={text}
          />
          <Button
            disabled={busy || !text.trim()}
            onClick={() => void act(() => submitCollabTaskResult(code, task.id, text), true)}
          >
            {c.publishResult}
          </Button>
          {error ? (
            <p className="text-destructive" role="alert">
              {error}
            </p>
          ) : null}
        </DialogContent>
      </Dialog>
      {!open && error ? (
        <p className="text-destructive" role="alert">
          {error}
        </p>
      ) : null}
    </>
  )
}
