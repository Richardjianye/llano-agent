import assert from 'node:assert/strict'

import { test } from 'vitest'

import { denyPopupsForCreatedWebContents, installWindowOpenDeny } from './window-open-policy'

type Handler = (details: { url: string }) => { action: 'deny' }

function fakeWebContents() {
  let handler: Handler | null = null

  return {
    contents: {
      setWindowOpenHandler(next: Handler) {
        handler = next
      }
    },
    invoke(url: string) {
      assert.ok(handler)

      return handler({ url })
    }
  }
}

test('trusted app links may be forwarded but the Electron child window is always denied', () => {
  const fake = fakeWebContents()
  const forwarded: string[] = []

  installWindowOpenDeny(fake.contents, url => forwarded.push(url))

  assert.deepEqual(fake.invoke('https://example.com/docs'), { action: 'deny' })
  assert.deepEqual(forwarded, ['https://example.com/docs'])
})

test('a forwarding callback failure cannot bypass the explicit popup denial', () => {
  const fake = fakeWebContents()

  installWindowOpenDeny(fake.contents, () => {
    throw new Error('forwarding failed')
  })

  assert.deepEqual(fake.invoke('https://example.com/docs'), { action: 'deny' })
})

test('new BrowserWindow and webview contents receive the default deny policy', () => {
  const fake = fakeWebContents()

  denyPopupsForCreatedWebContents({}, fake.contents)

  assert.deepEqual(fake.invoke('https://attacker.invalid/popup'), { action: 'deny' })
})
