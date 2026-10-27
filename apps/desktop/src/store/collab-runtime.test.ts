import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getCollabProjects: vi.fn(),
  getCollabMessages: vi.fn(),
  getCollabMembers: vi.fn(),
  getCollabTasks: vi.fn(),
  getCollabTurns: vi.fn(),
  syncCollabMessages: vi.fn(),
  tickCollabAgent: vi.fn(),
  touchCollabAgent: vi.fn(),
  resetCollabSession: vi.fn()
}))
vi.mock('@/hermes', () => api)
let stop: (() => void) | undefined
beforeEach(() => {
  vi.resetModules()
  vi.resetAllMocks()
  vi.useFakeTimers()
  api.getCollabProjects.mockResolvedValue({
    authenticated: true,
    projects: [{ code: 'a', status: 'active', message_seq: 40 }],
    stale: false
  })
  api.getCollabMembers.mockResolvedValue({
    members: [{ id: 2, is_me: true, member_kind: 'agent', respond_to: 'project_members', user_uid: 'me' }],
    stale: false
  })
  api.getCollabTasks.mockResolvedValue({ tasks: [], stale: false })
  api.getCollabTurns.mockResolvedValue({ turns: [], pending: [], availability: [] })
  api.syncCollabMessages.mockResolvedValue({ messages: [], next_seq: 40, stopped: false })
  api.tickCollabAgent.mockResolvedValue({ ran: false })
})
afterEach(() => {
  stop?.()
  stop = undefined
  vi.useRealTimers()
})
async function start() {
  const state = await import('./collab')
  const runtime = await import('./collab-runtime')
  stop = runtime.startCollabRuntime()
  await vi.advanceTimersByTimeAsync(0)
  return { state, runtime }
}
it('receives for an unselected project and refreshes shared states while a model call is pending', async () => {
  api.tickCollabAgent.mockReturnValue(new Promise(() => {}))
  const { state } = await start()
  expect(state.$collabSelected.get()).toBe('')
  expect(api.tickCollabAgent).toHaveBeenCalledTimes(1)
  api.getCollabTurns.mockResolvedValue({ turns: [{ id: 7, status: 'running' }], pending: [], availability: [] })
  await vi.advanceTimersByTimeAsync(1600)
  expect(state.$collabState.get().a.turns[0].status).toBe('running')
  expect(api.tickCollabAgent).toHaveBeenCalledTimes(1)
})
it('counts only new background messages, not initial history', async () => {
  const { state } = await start()
  expect(api.syncCollabMessages).toHaveBeenCalledWith('a', 40)
  api.syncCollabMessages.mockResolvedValue({ messages: [{ seq: 41, content: 'new' }], next_seq: 41 })
  await vi.advanceTimersByTimeAsync(3100)
  expect(state.$collabUnread.get().a).toBe(1)
  expect(state.$collabState.get().a.messages).toHaveLength(1)
})
it('revocation evicts cached messages and stops project polling', async () => {
  const { state } = await start()
  state.$collabDrafts.setKey('a', 'private draft')
  state.$collabPendingConfirm.set({
    code: 'a',
    taskId: 1,
    confirmation: { title: 'Task', body: 'Original', untrusted: true }
  })
  api.syncCollabMessages.mockResolvedValue({ stopped: true, reason: 'revoked', messages: [] })
  await vi.advanceTimersByTimeAsync(1600)
  const calls = api.syncCollabMessages.mock.calls.length
  await vi.advanceTimersByTimeAsync(5000)
  expect(api.syncCollabMessages).toHaveBeenCalledTimes(calls)
  expect(state.$collabState.get().a.stoppedReason).toBe('revoked')
  expect(state.$collabDrafts.get().a).toBe('')
  expect(state.$collabPendingConfirm.get()).toBeNull()
})
it('reset invalidates an in-flight response before it can restore old account content', async () => {
  let resolve!: (value: unknown) => void
  api.syncCollabMessages.mockReturnValue(
    new Promise(r => {
      resolve = r
    })
  )
  const { state, runtime } = await start()
  await state.resetCollab()
  resolve({ messages: [{ seq: 41, content: 'old account' }] })
  await vi.advanceTimersByTimeAsync(2000)
  expect(state.$collabState.get()).toEqual({})
  expect(runtime.$collabRuntimeActive.get()).toBe(false)
})
it('pause prevents new execution while reads continue', async () => {
  const { runtime } = await start()
  runtime.$collabReceiving.set(false)
  const calls = api.tickCollabAgent.mock.calls.length
  await vi.advanceTimersByTimeAsync(3100)
  expect(api.tickCollabAgent).toHaveBeenCalledTimes(calls)
  expect(api.syncCollabMessages.mock.calls.length).toBeGreaterThan(1)
})

it('a fresh authorized project listing resumes reception after removal and rejoin', async () => {
  const { state } = await start()
  api.syncCollabMessages.mockResolvedValue({ stopped: true, reason: 'revoked', messages: [] })
  await vi.advanceTimersByTimeAsync(1600)
  api.syncCollabMessages.mockResolvedValue({ messages: [{ seq: 41, content: 'rejoined' }] })
  await vi.advanceTimersByTimeAsync(6500)
  expect(state.$collabState.get().a.messages).toMatchObject([{ content: 'rejoined' }])
  expect(state.$collabState.get().a.stoppedReason).toBe('')
})

it('a transient initial list failure retries instead of treating the user as signed out', async () => {
  api.getCollabProjects.mockRejectedValueOnce(new Error('sidecar starting'))
  const { runtime } = await start()
  expect(runtime.$collabRuntimeActive.get()).toBe(true)
  expect(api.syncCollabMessages).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(8000)
  expect(api.syncCollabMessages).toHaveBeenCalledWith('a', 40)
})
