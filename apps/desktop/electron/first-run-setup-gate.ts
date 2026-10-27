interface FirstRunSetupBackend {
  activeRoot?: string
  kind?: string
  platform?: string
}

interface FirstRunSetupGateOptions {
  // CUSTOM-BEGIN: 品牌分发不出这个选择（阶段 6）
  /**
   * 品牌构建里这道门必须整个不存在。
   *
   * 这个选择问的是「连接你自己已有的 Hermes 网关，还是在本机装一份」——
   * 两个答案在品牌分发里都不成立：网关是钉死的，本机安装走的是我们自己的
   * 分发渠道。真正的后果不是「多一个没用的界面」，而是**它排在品牌快速失败
   * 的前面**：sidecar 不在包里时 `resolveHermesBackend` 返回
   * `kind: 'bootstrap-needed'`，这道门就开始等一个渲染层根本没画出来的选择，
   * 于是主进程无限挂起，用户看到的是一个永远转下去的 CONNECTING，界面上
   * 连一个字的说明都没有（2026-08-25 在装了火绒/云枢的真机上实测到）。
   *
   * 关掉之后 `wait()` 立刻返回 `continue-local`，流程落到 `ensureRuntime()`，
   * 那里的品牌分支会抛「后端缺失，请从公司分发渠道重新安装」—— 一个说得清
   * 的错误，比一个转不完的圈好得多。
   */
  branded?: boolean
  // CUSTOM-END: 品牌分发不出这个选择
  hideChoice?: () => void
  log?: (message: string) => void
  onStuck?: (backend: FirstRunSetupBackend, stuckAfterMs: number) => void
  promptChoice?: (backend: FirstRunSetupBackend) => void
  stuckAfterMs?: number
}

export type FirstRunSetupDecision = 'continue-local' | 'remote-applied' | 'reset'

export function createFirstRunSetupGate({
  branded = false,
  hideChoice,
  log,
  onStuck,
  promptChoice,
  stuckAfterMs = 120000
}: FirstRunSetupGateOptions = {}) {
  let localBootstrapConfirmed = false

  let waiter: {
    promise: Promise<FirstRunSetupDecision>
    resolve: (decision: FirstRunSetupDecision) => void
  } | null = null

  let stuckTimer: ReturnType<typeof setTimeout> | null = null

  const clearStuckTimer = () => {
    if (stuckTimer) {
      clearTimeout(stuckTimer)
      stuckTimer = null
    }
  }

  const armStuckTimer = (backend: FirstRunSetupBackend) => {
    clearStuckTimer()

    if (!Number.isFinite(stuckAfterMs) || stuckAfterMs <= 0 || typeof log !== 'function') {
      return
    }

    stuckTimer = setTimeout(() => {
      onStuck?.(backend, stuckAfterMs)
      log(
        `[bootstrap] still waiting for first-run setup choice after ${Math.round(stuckAfterMs / 1000)}s ` +
          `(platform=${backend?.platform || 'unknown'})`
      )
    }, stuckAfterMs)

    if (typeof stuckTimer.unref === 'function') {
      stuckTimer.unref()
    }
  }

  const shouldGate = (backend?: FirstRunSetupBackend | null) =>
    // CUSTOM: `!branded` —— 见上面 options 里的说明
    !branded && Boolean(backend && backend.kind === 'bootstrap-needed' && !localBootstrapConfirmed)

  const wait = async (backend?: FirstRunSetupBackend | null) => {
    if (!shouldGate(backend)) {
      return 'continue-local' as const
    }

    if (waiter) {
      return waiter.promise
    }

    promptChoice?.(backend)
    armStuckTimer(backend)

    let resolveWaiter: (decision: FirstRunSetupDecision) => void = () => {}

    const promise = new Promise<FirstRunSetupDecision>(resolve => {
      resolveWaiter = resolve
    })

    waiter = { promise, resolve: resolveWaiter }

    return promise
  }

  const settleWaiter = (decision: FirstRunSetupDecision) => {
    clearStuckTimer()

    if (!waiter) {
      return false
    }

    const activeWaiter = waiter
    waiter = null
    activeWaiter.resolve(decision)

    return true
  }

  const continueLocal = () => {
    localBootstrapConfirmed = true
    settleWaiter('continue-local')
  }

  const resetForRetry = () => {
    // Reset paths are followed by a renderer reload / fresh startHermes() call.
    // Settle the old boot explicitly so it cannot fall through into local
    // bootstrap and cannot leak a forever-pending connection promise.
    settleWaiter('reset')
  }

  const resetForRepair = () => {
    resetForRetry()
    localBootstrapConfirmed = false
  }

  const abandonForRemoteApply = () => {
    // Resume the gated startHermes() with an explicit remote decision. The
    // caller re-resolves the newly-persisted remote config instead of falling
    // through into local bootstrap or leaking the original connection promise.
    const resumedWaiter = settleWaiter('remote-applied')

    if (!resumedWaiter) {
      return false
    }

    localBootstrapConfirmed = false
    hideChoice?.()

    return true
  }

  const isLocalBootstrapConfirmed = () => localBootstrapConfirmed
  const hasWaiter = () => Boolean(waiter)

  return {
    abandonForRemoteApply,
    continueLocal,
    hasWaiter,
    isLocalBootstrapConfirmed,
    resetForRepair,
    resetForRetry,
    shouldGate,
    wait
  }
}
