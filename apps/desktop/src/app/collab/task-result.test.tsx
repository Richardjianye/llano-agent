import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { zh } from '@/i18n/zh'
const api = vi.hoisted(() => ({ read: vi.fn(), publish: vi.fn(), latest: vi.fn() }))
vi.mock('@/hermes', () => ({
  getCollabTaskResult: api.read,
  submitCollabTaskResult: api.publish,
  acceptCollabTaskResult: vi.fn()
}))
vi.mock('./task-execution', () => ({ taskBinding: () => ({ storedId: 'private' }), latestTaskReply: api.latest }))
vi.mock('@/app/open-session', () => ({ openSession: vi.fn() }))
vi.mock('@/store/collab', async () => {
  const { atom } = await import('nanostores')

  return { $collabAccountEpoch: atom(0), loadCollabHistory: async () => {}, refreshCollabTasks: async () => {} }
})
import type { CollabTask } from '@/types/hermes'

import { TaskResult } from './task-result'
const task = { id: 4, title: 'Task', status: 'active' } as CollabTask

function mount(value = task, readOnly = false) {
  return render(
    <MemoryRouter>
      <I18nProvider configClient={null} initialLocale="zh">
        <TaskResult canAccept code="project" mine readOnly={readOnly} task={value} />
      </I18nProvider>
    </MemoryRouter>
  )
}

afterEach(cleanup)
beforeEach(() => {
  vi.clearAllMocks()
  api.latest.mockResolvedValue('Private reviewed reply')
  api.publish.mockResolvedValue({})
})
it('loading a private reply only fills an editable preview; publishing requires an explicit click', async () => {
  mount()
  fireEvent.click(screen.getByText(zh.collab.functional.submitResult))
  fireEvent.click(screen.getByText(zh.collab.functional.loadResult))
  const preview = await screen.findByDisplayValue('Private reviewed reply')
  expect(api.publish).not.toHaveBeenCalled()
  fireEvent.change(preview, { target: { value: 'Only this edited result' } })
  fireEvent.click(screen.getByText(zh.collab.functional.publishResult))
  expect(api.publish).toHaveBeenCalledWith('project', 4, 'Only this edited result')
})
it('read-only members can review historical public results without mutation controls', async () => {
  api.read.mockResolvedValue({ message: { content: 'Old public result' } })
  mount({ ...task, status: 'submitted', result_message_id: 80 }, true)
  expect(screen.queryByText(zh.collab.functional.acceptResult)).toBeNull()
  expect(screen.queryByText(zh.collab.functional.submitResult)).toBeNull()
  fireEvent.click(screen.getByText(zh.collab.functional.reviewResult))
  expect(await screen.findByText('Old public result')).toBeTruthy()
  expect(api.read).toHaveBeenCalledWith('project', 4)
})
