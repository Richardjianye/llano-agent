/**
 * reconnect-escalation.ts
 *
 * Decides when a failing gateway reconnect loop must stop being invisible and
 * hand the screen to the recovery overlay.
 *
 * Without this the loop is genuinely unbounded: a dropped gateway keeps
 * redialing behind the fullscreen CONNECTING screen forever, with no way to
 * reach Settings, sign in, or switch to a local gateway. That is the
 * "lost connection bricks the app" dead end. Escalating raises a recoverable
 * boot error, which is what puts BootFailureOverlay (Retry / Repair / Gateway
 * settings / Open logs) on screen.
 *
 * ## Why wall-clock and not an attempt count
 *
 * The reconnect delay is full-jitter (`reconnect-backoff.ts`), so attempt
 * counts are a meaningless clock: six jittered attempts can elapse in ~9s,
 * while the old deterministic 1→15s ladder took ~45s to reach six failures.
 * Counting attempts would make the user-visible timeout swing by 5x depending
 * on which random delays came up. The threshold below preserves the original
 * ~45s calibration regardless of how the jitter falls.
 *
 * Extracted as a dependency-free pure predicate (same idiom as
 * `electron/backend-start-failure.ts`) so the fault matrix can assert the
 * bound without mounting the gateway boot hook — see
 * `src/app/product-fault-matrix.test.tsx`.
 */

/** How long the reconnect loop may keep failing before the recovery surface takes over. */
export const RECONNECT_ESCALATE_AFTER_MS = 45_000

export interface ReconnectEscalationContext {
  /**
   * Wall-clock start of the current disconnect episode — the first failed
   * reconnect attempt. `null` while healthy: a loop that has not failed yet
   * has nothing to escalate.
   */
  failingSince: null | number
  /** Now, in the same clock as `failingSince`. */
  now: number
  /** True once this episode has already escalated; escalation is one-shot. */
  alreadyEscalated: boolean
}

/**
 * Whether this reconnect failure should raise the recovery overlay.
 *
 * One-shot per episode: `alreadyEscalated` keeps a loop that is still failing
 * from re-raising the boot error on every backoff tick, which would restack
 * the overlay's error toast (and its haptics) every few seconds.
 */
export function shouldEscalateReconnect({
  alreadyEscalated,
  failingSince,
  now
}: ReconnectEscalationContext): boolean {
  if (alreadyEscalated || failingSince === null) {
    return false
  }

  return now - failingSince >= RECONNECT_ESCALATE_AFTER_MS
}
