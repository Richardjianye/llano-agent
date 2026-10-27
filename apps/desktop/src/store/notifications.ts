import { atom } from 'nanostores'

import { translateNow } from '@/i18n'
import { IS_BRANDED_PRODUCT } from '@/lib/product-policy'

export type NotificationKind = 'error' | 'warning' | 'info' | 'success'

export interface NotificationAction {
  label: string
  onClick: () => void
}

export type NotificationPlacement = 'default' | 'bottom-right'

export interface AppNotification {
  id: string
  kind: NotificationKind
  /** When set, renders this codicon instead of the default kind icon. */
  icon?: string
  /** When set, tints the icon and message with this CSS color (severity ramp). */
  accentColor?: string
  /** Secondary detail line rendered below the message, muted (e.g. "$220.00 cap"). */
  meta?: string
  title?: string
  message: string
  detail?: string
  action?: NotificationAction
  onDismiss?: () => void
  createdAt: number
  placement?: NotificationPlacement
}

export interface NotificationInput {
  id?: string
  kind?: NotificationKind
  icon?: string
  accentColor?: string
  meta?: string
  title?: string
  message: string
  detail?: string
  action?: NotificationAction
  onDismiss?: () => void
  durationMs?: number
  placement?: NotificationPlacement
}

let notificationCounter = 0
const timers = new Map<string, number>()

export const $notifications = atom<AppNotification[]>([])

function defaultDuration(kind: NotificationKind) {
  if (kind === 'error' || kind === 'warning') {
    return 0
  }

  return 5_000
}

// Only interruptions worth a top-center toast: errors, warnings, and anything
// with an action button the user needs to notice and click (restart gateway,
// update available, sign-in prompts). Everything else — the bulk of routine
// "saved"/"enabled"/"archived" confirmations across settings, MCP, cron,
// profiles, messaging — is ambient feedback and defaults to a quiet
// bottom-right toast instead. Callers can still force `placement: 'default'`
// for a specific case.
function defaultPlacement(kind: NotificationKind, action?: NotificationAction): NotificationPlacement {
  if (kind === 'error' || kind === 'warning' || action) {
    return 'default'
  }

  return 'bottom-right'
}

function cleanErrorText(value: string) {
  return value.replace(/^Error:\s*/, '').trim()
}

/** True when an error string is a disk-full / ENOSPC / SQLITE_FULL failure. */
export function isDiskFullErrorMessage(message: string): boolean {
  return (
    /no space left on device/i.test(message) ||
    /not enough space/i.test(message) ||
    /database or disk is full/i.test(message) ||
    /\bENOSPC\b/i.test(message) ||
    /disk full/i.test(message) ||
    /full disk/i.test(message)
  )
}

const ERROR_SUMMARIES: { test: (msg: string) => boolean; summarize: (msg: string) => string }[] = [
  {
    // Branded builds: the agent refuses to send when it cannot fetch the
    // redaction policy from the company gateway. Failing closed is exactly
    // right (decision #9 — never send unredacted text), but the raw message
    // is developer English and reaches business users as a screenshot.
    // Same class as the `HTTP 400: 缺少 model 参数` we fixed in stage 4.
    test: msg => /redaction policy (expired|unavailable)/i.test(msg),
    summarize: () => translateNow('productGateway.redactionUnavailable')
  },
  {
    // 网关停着时的第二发。第一条消息撞 fail-closed 的 redaction policy，
    // 重试那条连 TCP 都建不起来，OpenAI SDK 抛的是一句光秃秃的
    // `Connection error.` —— 同样是开发者英文，同样落在业务人员眼前。
    //
    // ⚠️ 这条**必须按品牌构建收窄**，和上面那条不同：`redaction policy` 这个词
    // 只可能出自我们自己的 fail-closed，天然自限；而「连接失败」在 stock Hermes
    // 里可以来自用户自己配的任何一个 provider，把它一律说成「公司网关」是撒谎。
    test: msg => IS_BRANDED_PRODUCT && /^connection error\.?$/i.test(msg.trim()),
    summarize: () => translateNow('productGateway.redactionUnavailable')
  },
  {
    // 网关的鉴权失败原文（「Error code: 401 - API Key 已吊销」「账号已被禁用」）
    // 会原样落进聊天气泡。网关那几个字不能改 —— 已发出去的客户端靠它们判断要不要
    // 重新校验登录（lib/product-auth.ts）—— 所以在显示这一侧换成人话。
    // 同时发生的重新校验会把人带回登录页，这里只负责别让气泡里是一串技术原文。
    // 只认网关自己的措辞：光看 401 会把本地 API_SERVER_KEY 之类的错也吞掉。
    test: msg =>
      IS_BRANDED_PRODUCT && /API Key (?:无效|已吊销|已过期)|账号已被禁用|账户被禁用|账号已停用|身份凭证已?失效/.test(msg),
    summarize: () => translateNow('productGateway.credentialInvalid')
  },
  {
    // Disk full / ENOSPC — session DB write, backend crash, or any path that
    // bubbles "no space left" / SQLITE_FULL through notifyError. Match before
    // generic length truncation so the user gets a clear "free space" toast
    // instead of a silent send or a raw errno dump.
    test: isDiskFullErrorMessage,
    summarize: () => translateNow('notifications.errors.diskFull')
  },
  {
    test: msg => /['"]code['"]\s*:\s*['"]gateway_auth_failed['"]/i.test(msg),
    summarize: () => translateNow('notifications.errors.gatewayAuthFailed')
  },
  {
    test: msg => /incorrect api key provided/i.test(msg) || /['"]code['"]\s*:\s*['"]invalid_api_key['"]/i.test(msg),
    summarize: msg => {
      const status = msg.match(/(?:error code|status(?:Code)?)[^\d]*(\d{3})/i)?.[1]

      return status
        ? translateNow('notifications.errors.openaiRejectedApiKeyWithStatus', status)
        : translateNow('notifications.errors.openaiRejectedApiKey')
    }
  },
  {
    test: msg => /neither voice_tools_openai_key nor openai_api_key is set/i.test(msg),
    summarize: () => translateNow('notifications.errors.openaiTtsNeedsKey')
  },
  {
    test: msg => /ELEVENLABS_API_KEY not set/i.test(msg) || /ElevenLabs STT API error \(HTTP 401\)/i.test(msg),
    summarize: msg =>
      /ELEVENLABS_API_KEY not set/i.test(msg)
        ? translateNow('notifications.errors.elevenLabsNeedsKey')
        : translateNow('notifications.errors.elevenLabsRejectedKey')
  },
  {
    test: msg => /method not allowed/i.test(msg),
    summarize: () => translateNow('notifications.errors.methodNotAllowed')
  },
  {
    test: msg => /microphone permission/i.test(msg),
    summarize: () => translateNow('notifications.errors.microphonePermission')
  }
]

/**
 * Turn a raw error string into something a business user can act on.
 *
 * Exported so the inline chat-bubble path (`inlineErrorMessage`) runs the same
 * table as the toast path. They were separate, which is how
 * `redaction policy expired` reached users verbatim: the rule existed for
 * toasts but a failed send renders in the transcript, not a toast.
 */
export function summarizeErrorMessage(message: string, fallback: string) {
  const rule = ERROR_SUMMARIES.find(r => r.test(message))

  if (rule) {
    return rule.summarize(message)
  }

  return message.length > 180 ? fallback : message || fallback
}

function readableError(error: unknown, fallback: string): { message: string; detail?: string } {
  const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : fallback
  const unwrapped = raw.match(/Error invoking remote method '[^']+': Error: (.+)$/)?.[1] ?? raw
  const cleaned = cleanErrorText(unwrapped)
  const detail = cleaned.match(/"detail"\s*:\s*"([^"]+)"/)?.[1] ?? cleaned
  const summary = summarizeErrorMessage(detail, fallback)

  return { message: summary, detail: detail === summary ? undefined : detail }
}

export function notify(input: NotificationInput): string {
  const kind = input.kind ?? 'info'
  const id = input.id ?? `${Date.now()}-${notificationCounter++}`

  const notification: AppNotification = {
    id,
    kind,
    icon: input.icon,
    accentColor: input.accentColor,
    meta: input.meta,
    title: input.title,
    message: input.message,
    detail: input.detail,
    action: input.action,
    onDismiss: input.onDismiss,
    createdAt: Date.now(),
    placement: input.placement ?? defaultPlacement(kind, input.action)
  }

  window.clearTimeout(timers.get(id))
  timers.delete(id)
  $notifications.set([notification, ...$notifications.get().filter(item => item.id !== id)].slice(0, 4))

  const duration = input.durationMs ?? defaultDuration(kind)

  if (duration > 0) {
    timers.set(
      id,
      window.setTimeout(() => dismissNotification(id), duration)
    )
  }

  return id
}

export function notifyError(error: unknown, fallback: string): string {
  const readable = readableError(error, fallback)

  return notify({
    kind: 'error',
    title: fallback,
    message: readable.message,
    detail: readable.detail
  })
}

export function dismissNotification(id: string) {
  window.clearTimeout(timers.get(id))
  timers.delete(id)
  const dismissed = $notifications.get().find(item => item.id === id)
  $notifications.set($notifications.get().filter(item => item.id !== id))
  dismissed?.onDismiss?.()
}

export function clearNotifications() {
  for (const timer of timers.values()) {
    window.clearTimeout(timer)
  }

  timers.clear()
  const all = $notifications.get()
  $notifications.set([])

  for (const item of all) {
    item.onDismiss?.()
  }
}
