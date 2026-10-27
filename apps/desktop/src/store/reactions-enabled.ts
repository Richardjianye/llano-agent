/**
 * Message reactions (iMessage-style tapbacks) — 默认开。
 *
 * 上游默认关，理由是每条消息都多出 ☺ 槽位和右键选择器。我们这边是内部办公场景，
 * 回应比“少一个图标”有用，所以默认打开；用户关掉后写入 'off'，不会被这个默认覆盖。
 *
 * 上游原注： reactions add affordances to every message row (the ☺ slot,
 * right-click pickers, :shortcode: completions), and the agent gains a tool
 * that reacts to your messages. Presentation-scoped, so the renderer owns it
 * (desktop AGENTS.md: state lives with its authority).
 *
 * Gates the UI only — persisted reactions still render if the data exists
 * (a reaction you set before turning it off shouldn't vanish from history).
 */

import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'
import { activeGateway } from '@/store/gateway'

const KEY = 'hermes.desktop.reactions.v1'

// 没存过 = 新装 -> 开。只有显式存了 'off' 才关，所以用户的选择永远赢过这个默认。
export const $reactionsEnabled = atom<boolean>(
  typeof window === 'undefined' ? false : storedString(KEY) !== 'off'
)

export function setReactionsEnabled(enabled: boolean): void {
  $reactionsEnabled.set(enabled)
}

if (typeof window !== 'undefined') {
  // listen, not subscribe: fire on CHANGE only, so app startup doesn't write
  // config.set (or clobber a profile's setting with another window's default).
  $reactionsEnabled.listen(enabled => {
    persistString(KEY, enabled ? 'on' : 'off')
    // Mirror into gateway config: the backend gates the agent's
    // react_to_message tool and the model-context annotation on
    // display.message_reactions, so the renderer toggle is the one lever.
    void activeGateway()
      ?.request('config.set', { key: 'display.message_reactions', value: enabled ? 'true' : 'false' })
      .catch(() => {
        // Not connected yet — the next toggle (or default-off) still holds.
      })
  })
}
