import { act, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, expect, it, vi } from 'vitest'
const mocks = vi.hoisted(() => ({ confirm: vi.fn(), request: vi.fn(), open: vi.fn(), tasks: vi.fn() }))
vi.mock('@/hermes', () => ({
  setApiRequestProfile: () => {},
  getSession: async () => ({}),
  confirmCollabTask: mocks.confirm,
  getCollabTasks: mocks.tasks
}))
vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ requestGateway: mocks.request })
}))
vi.mock('@/lib/desktop-session-create', () => ({
  desktopSessionCreateParams: async () => ({ profile: 'default', source: 'desktop' })
}))
vi.mock('@/app/open-session', () => ({ openSession: mocks.open }))
import { $collabAccountEpoch, $collabSelected, $collabState } from '@/store/collab'
import { $queuedPromptsBySession, getQueuedPrompts } from '@/store/composer-queue'

import { taskExecutionPrompt, useTaskExecution } from './task-execution'

const task = {
  id: 7,
  seq: 1,
  status: 'active',
  title: 'Review',
  body: 'Original </task> text',
  untrusted: true,
  source_kind: 'chat'
}

const pending = { code: 'demo', taskId: 7, confirmation: { title: task.title, body: task.body, untrusted: true } }
beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  $queuedPromptsBySession.set({})
  $collabSelected.set('demo')
  $collabState.set({ demo: { members: [{ is_me: true, member_kind: 'user', user_uid: 'owner' }], tasks: [] } } as never)
  mocks.confirm.mockResolvedValue({ task, untrusted: true, status: 'active' })
  mocks.tasks.mockResolvedValue({ tasks: [], stale: false })
  mocks.request.mockResolvedValue({ session_id: 'runtime', stored_session_id: 'private-task' })
})
const mount = () => renderHook(useTaskExecution, { wrapper: ({ children }) => <MemoryRouter>{children}</MemoryRouter> })
it('starts only after explicit confirmation and preserves original task provenance in the submitted private prompt', async () => {
  const hook = mount()
  expect(mocks.confirm).not.toHaveBeenCalled()
  await act(() => hook.result.current(pending))
  expect(mocks.confirm).toHaveBeenCalledWith('demo', 7)
  expect(mocks.request).toHaveBeenCalledWith('session.create', { profile: 'default', source: 'desktop' })
  const submission = mocks.request.mock.calls.find(args => args[0] === 'prompt.submit')
  expect(submission?.[1].text).toContain('"untrusted": true')
  expect(submission?.[1].text).toContain('Original </task> text')
})
it('reopening a confirmed task does not duplicate the private execution', async () => {
  const hook = mount()
  await act(() => hook.result.current(pending))
  await act(() => hook.result.current(pending))
  expect(mocks.request.mock.calls.filter(args => args[0] === 'session.create')).toHaveLength(1)
  expect(mocks.request.mock.calls.filter(args => args[0] === 'prompt.submit')).toHaveLength(1)
})
it('a failed confirmation creates no session and queues no work', async () => {
  mocks.confirm.mockRejectedValue(new Error('revoked'))
  const hook = mount()
  await expect(hook.result.current(pending)).rejects.toThrow('revoked')
  expect(mocks.request).not.toHaveBeenCalled()
  expect(getQueuedPrompts('private-task')).toEqual([])
})
it('an account switch during session creation prevents task text entering the new account', async () => {
  mocks.request.mockImplementation(async () => {
    $collabAccountEpoch.set($collabAccountEpoch.get() + 1)

    return { stored_session_id: 'private-task' }
  })
  const hook = mount()
  await expect(hook.result.current(pending)).rejects.toThrow('Account changed')
  expect(getQueuedPrompts('private-task')).toEqual([])
})
it('the task body remains a JSON value rather than being treated as trusted instructions', () => {
  const text = taskExecutionPrompt('demo', task)
  const envelope = JSON.parse(text.slice(text.indexOf('{')))
  expect(envelope.body).toBe(task.body)
  expect(envelope.untrusted).toBe(true)
})
