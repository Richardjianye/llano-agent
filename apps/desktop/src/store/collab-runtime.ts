import { atom } from 'nanostores'

import {
  getCollabMembers,
  getCollabTasks,
  getCollabTurns,
  syncCollabMessages,
  tickCollabAgent,
  touchCollabAgent
} from '@/hermes'
import {
  $collabAccountEpoch,
  $collabAuthenticated,
  $collabProjects,
  $collabProjectsStale,
  $collabSelected,
  $collabState,
  $collabUnread,
  $collabVisible,
  applyCollabSnapshot,
  forgetCollabProject,
  loadCollabProjects,
  myAgents,
  stopCollabPolling,
  stopCollabTicking
} from '@/store/collab'

export const $collabReceiving = atom(true)
export const $collabRuntimeActive = atom(false)

/** One read loop and at most one local execution per project. The gateway lease
 * still arbitrates across windows/devices. Visibility never owns execution. */
export function startCollabRuntime(): () => void {
  let alive = true
  const epoch = $collabAccountEpoch.get()
  const workers = new Map<string, { stop: () => void; isActive: () => boolean }>()
  let timer: ReturnType<typeof setTimeout> | undefined
  $collabRuntimeActive.set(true)
  stopCollabPolling()
  stopCollabTicking()
  const valid = () => alive && epoch === $collabAccountEpoch.get()

  function worker(code: string, baseline: number) {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let executing = false
    let lastMetadata = 0
    let cursor = baseline
    const live = () => valid() && active
    const current = () => $collabState.get()[code]
    const isRead = () => $collabVisible.get() && $collabSelected.get() === code

    async function execute() {
      if (!live() || executing || !$collabReceiving.get()) {
        return
      }

      executing = true

      try {
        for (const agent of myAgents(current()?.members ?? [])) {
          if (!live() || !$collabReceiving.get()) {
            break
          }

          if (agent.respond_to === 'nobody') {
            continue
          }

          try {
            // The request only asks the server to inspect pending events. It cannot
            // manufacture a trigger or repeat a consumed one.
            const result = await tickCollabAgent(code, agent.id, 0)

            if (live() && result.ran) {
              applyCollabSnapshot(code, { agentError: '' })
            }
          } catch (error) {
            if (live()) {
              applyCollabSnapshot(code, { agentError: String(error instanceof Error ? error.message : error) })
            }
          }
        }
      } finally {
        executing = false
      }
    }

    const poll = async () => {
      if (!live()) {
        return
      }

      try {
        const messages = await syncCollabMessages(code, cursor)

        if (!live()) {
          return
        }

        if (messages.stopped) {
          active = false
          forgetCollabProject(code, messages.reason || 'revoked')

          return
        }

        if (messages.offline) {
          throw new Error('collab-messages-unavailable')
        }

        const turns = await getCollabTurns(code)

        if (!live()) {
          return
        }

        const fresh = (messages.messages ?? []).filter(m => m.seq > cursor)
        cursor = Math.max(cursor, ...fresh.map(m => m.seq))

        if (!isRead() && fresh.length) {
          $collabUnread.setKey(code, ($collabUnread.get()[code] || 0) + fresh.length)
        }

        applyCollabSnapshot(code, {
          messages: fresh,
          nextSeq: cursor,
          stale: false,
          stoppedReason: '',
          ...(turns.stale
            ? { turnsStale: true }
            : {
                turns: turns.turns || [],
                pending: turns.pending || [],
                availability: turns.availability || [],
                turnsStale: false,
                hops: turns.hops,
                hopLimit: turns.hop_limit
              })
        })

        if (Date.now() - lastMetadata >= 8_000 || !current()?.members.length) {
          const [members, tasks] = await Promise.all([getCollabMembers(code), getCollabTasks(code)])

          if (!live()) {
            return
          }

          applyCollabSnapshot(code, {
            ...(!members.stale ? { members: members.members } : {}),
            ...(!tasks.stale ? { tasks: tasks.tasks } : {})
          })
          lastMetadata = Date.now()

          if ($collabReceiving.get()) {
            await Promise.allSettled(
              myAgents(members.members)
                .filter(m => m.respond_to !== 'nobody')
                .map(m => touchCollabAgent(code, m.id))
            )
          }
        }

        if (live()) {
          void execute()
        }
      } catch (error) {
        if (!live()) {
          return
        }

        applyCollabSnapshot(code, {
          stale: true,
          turnsStale: true,
          error: error instanceof Error ? error.message : String(error)
        })
      }

      if (live()) {
        timer = setTimeout(() => void poll(), current()?.stale ? 8_000 : 1_500)
      }
    }

    void poll()

    return {
      isActive: () => active,
      stop: () => {
        active = false

        if (timer) {
          clearTimeout(timer)
        }
      }
    }
  }

  const refresh = async () => {
    if (!valid()) {
      return
    }

    try {
      await loadCollabProjects()

      if (!valid()) {
        return
      }

      if (!$collabProjectsStale.get() && !$collabAuthenticated.get()) {
        stop()

        return
      }

      if (!$collabProjectsStale.get()) {
        const projects = $collabProjects.get().filter(p => p.status === 'active' || !p.status)

        for (const [code, runner] of workers) {
          if (!runner.isActive() || !projects.some(p => p.code === code)) {
            runner.stop()
            workers.delete(code)

            if (!$collabProjects.get().some(p => p.code === code)) {
              forgetCollabProject(code, 'revoked')
            }
          }
        }

        for (const project of projects) {
          if (!workers.has(project.code)) {
            workers.set(
              project.code,
              worker(project.code, $collabState.get()[project.code]?.nextSeq ?? project.message_seq ?? 0)
            )
          }
        }
      }
    } catch {
      /* List failures retain the known projects. Per-project auth is checked on every call. */
    }

    if (valid()) {
      timer = setTimeout(() => void refresh(), 8_000)
    }
  }

  function stop() {
    alive = false

    if (timer) {
      clearTimeout(timer)
    }

    workers.forEach(w => w.stop())
    workers.clear()
    $collabRuntimeActive.set(false)
  }

  const unlisten = $collabAccountEpoch.listen(() => stop())
  void refresh()

  return () => {
    unlisten()
    stop()
  }
}
