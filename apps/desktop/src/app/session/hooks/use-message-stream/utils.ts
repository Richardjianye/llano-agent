import type { GatewayEventPayload } from '@/lib/chat-messages'
import { normalizePersonalityValue } from '@/lib/chat-runtime'
import { summarizeErrorMessage } from '@/store/notifications'

import type { ClientSessionState } from '../../../types'

type SessionRuntimeStatePatch = Partial<
  Pick<
    ClientSessionState,
    'mode' | 'branch' | 'cwd' | 'fast' | 'model' | 'personality' | 'provider' | 'reasoningEffort' | 'serviceTier' | 'yolo'
  >
>

export function sessionInfoStatePatch(payload: GatewayEventPayload | undefined): SessionRuntimeStatePatch {
  const patch: SessionRuntimeStatePatch = {}

  if (payload?.mode === 'chat' || payload?.mode === 'work') {
    patch.mode = payload.mode
  }

  if (typeof payload?.model === 'string') {
    patch.model = payload.model || ''
  }

  if (typeof payload?.provider === 'string') {
    patch.provider = payload.provider || ''
  }

  if (typeof payload?.cwd === 'string') {
    patch.cwd = payload.cwd
  }

  if (typeof payload?.branch === 'string') {
    patch.branch = payload.branch
  }

  if (typeof payload?.personality === 'string') {
    patch.personality = normalizePersonalityValue(payload.personality)
  }

  if (typeof payload?.reasoning_effort === 'string') {
    patch.reasoningEffort = payload.reasoning_effort
  }

  if (typeof payload?.service_tier === 'string') {
    patch.serviceTier = payload.service_tier
  }

  if (typeof payload?.fast === 'boolean') {
    patch.fast = payload.fast
  }

  if (typeof payload?.yolo === 'boolean') {
    patch.yolo = payload.yolo
  }

  return patch
}

export function hasSessionInfoStatePatch(patch: SessionRuntimeStatePatch): boolean {
  return Object.keys(patch).length > 0
}

// Minimum gap between two assistant-text flushes during a stream. Was 16ms
// (rAF only), which at typical LLM token rates of ~30-80 tok/sec meant every
// token got its own React commit + Streamdown markdown re-parse, scaling
// linearly with the growing last-block length. Bumping to 33ms lets ~2 tokens
// batch into one commit at 60 tok/sec without introducing visible lag on the
// streaming text (still 30 fps of visible text growth). Big perceived
// smoothness win on long messages with big trailing paragraphs; see
// `scripts/profile-typing-lag.md` for the measurement work behind this.
export const STREAM_DELTA_FLUSH_MS = 33

// Ceiling for the ADAPTIVE flush gap (see scheduleDeltaFlush). Under heavy
// multi-stream load the gap stretches to 3x the measured flush cost so the
// main thread stays responsive to input; this cap guarantees streaming text
// still visibly updates at least ~4x per second no matter the load.
export const MAX_STREAM_FLUSH_GAP_MS = 250

// Typewriter smoothing. Upstream providers (DeepSeek measured ~200ms cadence)
// push tokens in bursts of 10-25 at once; painting each burst whole makes the
// reply jump a clause at a time and then stall, which users read as "slow".
// Timer flushes instead reveal a share of the queued backlog proportional to
// the time since the previous flush, so a burst is spread over roughly this
// window — about one upstream burst gap, keeping added latency under it.
export const STREAM_SMOOTH_DRAIN_MS = 250
// Floor so a trailing few characters don't crawl out one per tick.
const STREAM_SMOOTH_MIN_CHARS = 2

/** How many UTF-16 units of `backlog` a smoothed flush reveals after
 * `elapsedMs`. Never splits a surrogate pair. */
export function smoothRevealLength(backlog: string, elapsedMs: number): number {
  const share = Math.min(1, Math.max(0, elapsedMs) / STREAM_SMOOTH_DRAIN_MS)
  let count = Math.max(STREAM_SMOOTH_MIN_CHARS, Math.ceil(backlog.length * share))

  if (count >= backlog.length) {
    return backlog.length
  }

  const code = backlog.charCodeAt(count - 1)

  if (code >= 0xd800 && code <= 0xdbff) {
    count += 1
  }

  return count
}

// Gateway/provider failures sometimes arrive as message.complete text instead
// of an explicit error event. Treat matches as inline assistant errors so they
// persist like real error events and don't get erased by hydrate fallback.
const COMPLETION_ERROR_PATTERNS = [
  /^API call failed after \d+ retries:/i,
  /^HTTP\s+\d{3}\b/i,
  /^(Provider|Gateway)\s+error:/i
]

export function completionErrorText(finalText: string): string | null {
  const text = finalText.trim()

  return text && COMPLETION_ERROR_PATTERNS.some(re => re.test(text)) ? text : null
}

/**
 * 一轮对话失败时，气泡里最终显示的那句话。
 *
 * 抽成纯函数是为了能脱离 React 测到**用户真正看到的那个字符串** ——
 * 上一轮只测了 `inlineErrorMessage(new Error('redaction policy expired'))`，
 * 函数本身是对的、测试全绿，可 fail-closed 的报错走的根本不是那条路：
 * 提交已经成功、turn 跑起来之后才由终止帧带回错误，落在
 * `completeAssistantMessage` 上，那里从来没跑过措辞表。于是同一句英文
 * 又一次原样递到业务人员面前。
 *
 * 教训和故障矩阵头注释里那条同源，只是深了一层：那次问的是「这个故障发生时
 * 哪个组件先察觉到」，这次要问的是「**这条错误最后由哪个组件渲染**」。
 *
 * fallback 传原文：没有规则命中就保持不变，别因为超长把技术细节换成一句
 * 笼统的失败提示 —— 排查时那往往是唯一线索。
 */
export function completionErrorMessage(
  failure: { error: string; partial: boolean } | undefined,
  finalText: string
): string | null {
  const raw = failure?.error ?? completionErrorText(finalText)

  return raw ? summarizeErrorMessage(raw, raw) : raw
}

export const SUBAGENT_EVENT_TYPES = new Set([
  'subagent.spawn_requested',
  'subagent.start',
  'subagent.thinking',
  'subagent.tool',
  'subagent.progress',
  'subagent.complete'
])

// Anonymous progress events that carry todos but no name still belong to the
// todo stream; named todo events are obviously routed there too.
export function toTodoPayload(payload: GatewayEventPayload | undefined): GatewayEventPayload | undefined {
  if (!payload) {
    return undefined
  }

  const isTodo = payload.name === 'todo' || (!payload.name && Object.hasOwn(payload, 'todos'))

  return isTodo ? { ...payload, name: 'todo', tool_id: payload.tool_id || 'todo-live' } : undefined
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function parseMaybeRecord(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try {
      return asRecord(JSON.parse(value))
    } catch {
      return {}
    }
  }

  return asRecord(value)
}

const firstString = (...candidates: unknown[]): string => {
  for (const v of candidates) {
    if (typeof v === 'string' && v) {
      return v
    }
  }

  return ''
}

export function delegateTaskPayloads(
  payload: GatewayEventPayload | undefined,
  phase: 'running' | 'complete',
  sourceEventType?: string
): Record<string, unknown>[] {
  if (payload?.name !== 'delegate_task') {
    return []
  }

  const args = parseMaybeRecord(payload.args ?? payload.input)
  const result = parseMaybeRecord(payload.result)
  const rawTasks = Array.isArray(args.tasks) ? args.tasks : []
  const tasks = rawTasks.length ? rawTasks.map(parseMaybeRecord) : [args]
  const status = phase === 'complete' ? (payload.error ? 'failed' : 'completed') : 'running'
  const toolId = payload.tool_id || payload.tool_call_id || payload.id || 'delegate_task'
  const progressText = firstString(payload.preview, payload.message, payload.context)

  const eventType =
    phase === 'complete'
      ? 'subagent.complete'
      : sourceEventType === 'tool.start'
        ? 'subagent.start'
        : 'subagent.progress'

  return tasks.map((task, index) => {
    const goal = firstString(task.goal, args.goal, payload.context) || 'Delegated task'
    const summary = firstString(result.summary, payload.summary, payload.message)

    return {
      depth: 0,
      duration_seconds: payload.duration_s,
      goal,
      status,
      subagent_id: `delegate-tool:${toolId}:${index}`,
      summary: summary || undefined,
      task_count: tasks.length,
      task_index: index,
      text: eventType === 'subagent.progress' ? progressText || goal : undefined,
      tool_name: eventType === 'subagent.start' ? 'delegate_task' : undefined,
      tool_preview: eventType === 'subagent.start' ? progressText : undefined,
      toolsets: Array.isArray(task.toolsets) ? task.toolsets : Array.isArray(args.toolsets) ? args.toolsets : [],
      event_type: eventType,
      output_tail:
        phase === 'complete' && summary
          ? [{ is_error: Boolean(payload.error), preview: summary, tool: 'delegate_task' }]
          : undefined
    }
  })
}
