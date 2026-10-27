/**
 * boot-failure-security.ts
 *
 * A boot failure caused by antivirus or policy refusing to start our child
 * processes arrives with ready-made, machine-specific instructions attached by
 * the main process (electron/windows-spawn-failure.ts → securityBlockAdvice).
 *
 * A branded build replaces the whole failure detail with one generic sentence so
 * diagnostics and local paths stay off a card people screenshot and forward.
 * That guard would also swallow the only part the user can act on, so the notice
 * is lifted back out here — and only the notice, never the per-component
 * `spawn EPERM` lines behind it.
 *
 * Matching on the message text is the idiom this screen already uses for remote
 * reauth failures (./boot-failure-reauth): the boot state carries a string.
 */

const MARKER = '环境准备被安全软件或组策略拦截'

/** The instructions, or null when this failure is something else entirely. */
export function runtimeBlockNotice(error: string | null | undefined): string | null {
  const text = String(error ?? '')
  const start = text.indexOf(MARKER)

  return start === -1 ? null : text.slice(start).trim() || null
}

/**
 * The directories to trust, for a copy button — the screen shows them in their
 * %VARIABLE% form and an antivirus trust list takes them as-is.
 */
export function blockedDirectories(notice: string): string[] {
  return notice
    .split('\n')
    .filter(line => /^\s{2,}\S/.test(line))
    .map(line => line.trim())
}
