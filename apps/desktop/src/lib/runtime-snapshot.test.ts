import { describe, expect, it } from 'vitest'

import { cacheRuntimeSnapshot } from './incremental-external-store-runtime'

describe('runtime snapshot compatibility', () => {
  it('reuses unchanged reads and exposes changed values without waiting for subscriptions', () => {
    let messages = [{ id: 'first', text: 'partial' }]
    let isRunning = true
    const api = { getState: () => ({ messages, isRunning }) }
    cacheRuntimeSnapshot(api)

    const initial = api.getState()
    expect(api.getState()).toBe(initial)

    messages = [{ id: 'first', text: 'complete' }]
    const streamed = api.getState()
    expect(streamed).not.toBe(initial)
    expect(streamed.messages[0].text).toBe('complete')
    expect(api.getState()).toBe(streamed)

    isRunning = false
    const settled = api.getState()
    expect(settled).not.toBe(streamed)
    expect(settled.isRunning).toBe(false)
    expect(settled.messages).toBe(streamed.messages)
    expect(api.getState()).toBe(settled)
  })
})
