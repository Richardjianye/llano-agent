import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useIncrementalExternalStoreRuntime } from './incremental-external-store-runtime'

describe('external runtime state snapshots', () => {
  it('keeps unchanged snapshots stable before any messages exist', () => {
    const adapter = { messageRepository: { messages: [], headId: null }, isRunning: false, onNew: async () => {} }
    const { result } = renderHook(() => useIncrementalExternalStoreRuntime(adapter))

    for (const part of [result.current.thread, result.current.thread.composer, result.current.threads]) {
      const first = part.getState()
      const next = part.getState()
      expect(next).toBe(first)
    }
  })
  it('publishes composer changes and keeps subsequent unchanged reads stable', () => {
    const adapter = { messageRepository: { messages: [], headId: null }, isRunning: false, onNew: async () => {} }
    const { result } = renderHook(() => useIncrementalExternalStoreRuntime(adapter))
    const composer = result.current.thread.composer
    const before = composer.getState()
    const unsubscribe = composer.subscribe(() => undefined)
    act(() => composer.setText('draft after login'))
    const after = composer.getState()
    expect(after.text).toBe('draft after login')
    expect(after).not.toBe(before)
    expect(composer.getState()).toBe(after)
    unsubscribe()
    expect(composer.getState()).toBe(after)
  })
})
