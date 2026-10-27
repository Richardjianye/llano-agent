import { AssistantRuntimeProvider, type ThreadMessage } from '@assistant-ui/react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { clearSessionDraft, stashSessionDraft, takeSessionDraft } from '@/store/composer'
import { $activeGatewayProfile, $newChatProfile } from '@/store/profile'
import { $connection } from '@/store/session'
import { useStableStockRuntime } from '@/test-support/use-stable-stock-runtime'

import type { QueueEditState } from '../composer-utils'

import { useComposerDraft } from './use-composer-draft'

const messages: ThreadMessage[] = []
let api: ReturnType<typeof useComposerDraft>
let composer: { setText: (text: string) => void; getState: () => { text: string } }
let queueEdit: QueueEditState | null = null

function Editor({ session }: { session: string | null }) {
  api = useComposerDraft({
    activeQueueSessionKey: session,
    focusKey: session,
    inputDisabled: false,
    queueEditRef: { current: queueEdit },
    sessionId: session
  })

  return <div contentEditable data-testid="editor" ref={api.editorRef} tabIndex={0} />
}

function Harness({ session }: { session: string | null }) {
  const runtime = useStableStockRuntime({ messages, isRunning: false, onNew: async () => {} })
  composer = runtime.thread.composer

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Editor session={session} />
    </AssistantRuntimeProvider>
  )
}

afterEach(() => {
  cleanup()
  queueEdit = null
  clearSessionDraft(null)
  $newChatProfile.set(null)
  $activeGatewayProfile.set('default')
  $connection.set(null)
})
describe('real assistant-ui composer draft lifetime', () => {
  it('restores new conversation typing after visiting an existing session and ignores an unfocused runtime reset', async () => {
    $connection.set(null)
    $newChatProfile.set(null)
    $activeGatewayProfile.set('default')
    clearSessionDraft(null)
    stashSessionDraft('existing', 'existing draft', [])
    const view = render(<Harness session={null} />)
    const editor = view.getByTestId('editor')
    act(() => api.insertText('新会话尚未发送'))
    editor.blur()
    act(() => composer.setText(''))
    expect(editor.textContent).toBe('新会话尚未发送')
    await waitFor(() => {
      expect(composer.getState().text.trim()).toBe('新会话尚未发送')
      expect(api.hasText).toBe(true)
    })
    view.rerender(<Harness session="existing" />)
    expect(editor.textContent).toBe('existing draft')
    view.rerender(<Harness session={null} />)
    expect(editor.textContent).toBe('新会话尚未发送')
    act(() => api.clearDraft())
    expect(editor.textContent).toBe('')
    view.rerender(<Harness session="existing" />)
    view.rerender(<Harness session={null} />)
    expect(editor.textContent).toBe('')
    expect(takeSessionDraft(null).text).toBe('')
  })
  it('isolates drafts by profile and remote gateway while persisting final DOM keystrokes', () => {
    $connection.set(null)
    $newChatProfile.set(null)
    $activeGatewayProfile.set('draft-one')
    clearSessionDraft(null)
    const view = render(<Harness session={null} />)
    const editor = view.getByTestId('editor')
    editor.textContent = 'profile one final DOM'
    editor.blur()
    act(() => composer.setText(''))
    expect(editor.textContent).toBe('profile one final DOM')
    act(() => $activeGatewayProfile.set('draft-two'))
    expect(editor.textContent).toBe('')
    editor.textContent = 'profile two'
    act(() => $activeGatewayProfile.set('draft-one'))
    expect(editor.textContent).toBe('profile one final DOM')
    act(() => $connection.set({ mode: 'remote', baseUrl: 'https://isolated.example' } as never))
    expect(editor.textContent).toBe('')
    act(() => $connection.set(null))
    expect(editor.textContent).toBe('profile one final DOM')
  })
})

it('queue editing pagehide preserves the ordinary draft under its scoped key', () => {
  $connection.set(null)
  $newChatProfile.set(null)
  $activeGatewayProfile.set('default')
  stashSessionDraft('queue-session', 'ordinary draft', [])
  queueEdit = { sessionKey: 'queue-session', draft: 'ordinary draft', attachments: [], entryId: 'queued' }
  const view = render(<Harness session="queue-session" />)
  view.getByTestId('editor').textContent = 'editing a queued prompt'
  act(() => window.dispatchEvent(new Event('pagehide')))
  expect(takeSessionDraft('queue-session').text).toBe('ordinary draft')
})
