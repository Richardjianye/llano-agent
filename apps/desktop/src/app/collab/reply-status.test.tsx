import { expect, it } from 'vitest'

import type { CollabPendingReply, CollabTurn } from '@/types/hermes'

import { replyRows } from './reply-status'
const pending = [{ owner_uid: 'a', agent_label: 'Assistant', trigger_seq: 2, status: 'queued' }] as CollabPendingReply[]

const turn = (status: string, extra = {}) =>
  ({
    trigger_reason: 'mention',
    id: 9,
    owner_uid: 'a',
    agent_label: 'Assistant',
    trigger_seq: 2,
    status,
    ...extra
  }) as CollabTurn

it('reconciles a pending placeholder into a single authoritative running row', () => {
  expect(replyRows([turn('running')], pending, 2)).toMatchObject([{ id: 9, status: 'running' }])
})
it('removes a completed placeholder but retains a visible empty-result outcome', () => {
  expect(replyRows([turn('completed')], pending, 2)).toEqual([])
  expect(replyRows([turn('completed', { error_code: 'empty_reply' })], pending, 2)[0].status).toBe('emptyReply')
})
it('late older snapshots never override the newest terminal state', () => {
  expect(replyRows([turn('cancelled'), turn('running', { id: 8 })], pending, 2)[0].status).toBe('cancelled')
  expect(replyRows([turn('running')], pending, 3)).toEqual([])
})

it('keeps owner-requested runs separate from messages that did not mention an agent', () => {
  const manual = turn('cancelled', { trigger_reason: 'owner_request' })
  expect(replyRows([manual], [], 2)).toEqual([])
  expect(replyRows([manual], [], undefined)).toEqual([])
  expect(replyRows([turn('running', { trigger_reason: 'owner_request' })], [], undefined)).toMatchObject([
    { status: 'running' }
  ])
  expect(replyRows([turn('running')], pending, undefined)).toEqual([])
})

it('does not revive an older manual run after cancellation', () => {
  expect(
    replyRows(
      [
        turn('running', { id: 8, trigger_reason: 'owner_request' }),
        turn('cancelled', { trigger_reason: 'owner_request' })
      ],
      [],
      undefined
    )
  ).toEqual([])
})
