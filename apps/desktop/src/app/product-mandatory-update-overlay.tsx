/**
 * 强制更新的全屏阻断遮罩（阶段 5 · U4）
 *
 * ## 只有三个出口
 *
 * 立即安装 / 重试 / 退出。刻意**没有**关闭按钮、没有「稍后再说」、没有 snooze，
 * 也不响应 ESC 和点击外部 —— 强制更新一旦能被推迟，它就只是一个语气重一点的
 * 提示。这与同目录下的 `updates-overlay.tsx` 正相反：那个是可选更新用的对话框，
 * 关掉它是正常操作。
 *
 * ## 判据只有一个来源
 *
 * `$mandatoryUpdate`，由 `store/product-mandatory-update.ts` 从**已验签清单**
 * 算出来的 `mandatory` 闩住。这里不重算、不看 `updatePolicy`、不比版本号：
 * 判定逻辑已经有三份实现（网关 Python、hermes_cli、Electron 主进程），
 * 再加第四份必然漂移。
 *
 * ## 它拦得住什么、拦不住什么
 *
 * 拦得住「用户没注意到必须更新」，拦不住「用户铁了心要用旧版」—— 渲染层里的
 * 任何东西都能被绕过。旧客户端真正用不了靠的是网关侧对每个请求的鉴权授权，
 * 这道遮罩负责的是让人看得见并且只剩一条路可走。
 */

import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { useI18n } from '@/i18n'
import { Loader2 } from '@/lib/icons'
import { PRODUCT_POLICY } from '@/lib/product-policy'
import { $mandatoryUpdate } from '@/store/product-mandatory-update'
import { $updateApply, applyUpdates, installStagedUpdate } from '@/store/updates'

export function ProductMandatoryUpdateOverlay() {
  const { t } = useI18n()
  const copy = t.productUpdate
  const blocking = useStore($mandatoryUpdate)
  const apply = useStore($updateApply)
  const containerRef = useRef<HTMLDivElement>(null)

  const installing = apply.applying || apply.stage === 'restart'
  const failed = apply.stage === 'error'
  // 后台已经把这一版下好了（Windows 强制更新）：安装本来就会在下次启动时自动
  // 做掉，所以这里不该再逼着他"立即安装"——改成告诉他已经下好，想快可以现在重启。
  const staged = !installing && !failed && Boolean(blocking?.pendingVersion) &&
    blocking?.pendingVersion === blocking?.targetVersion

  // 遮罩盖住了整个界面，但底下的输入框仍然是可 Tab 到的 —— 不管这一点的话，
  // 「全屏阻断业务」实际上只挡鼠标。焦点跑出去就拽回来。
  useEffect(() => {
    if (!blocking) {
      return
    }

    const container = containerRef.current
    container?.focus()

    const keepFocusInside = (event: FocusEvent) => {
      const node = container

      if (node && event.target instanceof Node && !node.contains(event.target)) {
        node.focus()
      }
    }

    document.addEventListener('focusin', keepFocusInside)

    return () => document.removeEventListener('focusin', keepFocusInside)
  }, [blocking])

  if (!blocking) {
    return null
  }

  const percent =
    typeof apply.percent === 'number' && Number.isFinite(apply.percent)
      ? Math.max(2, Math.min(100, Math.round(apply.percent)))
      : null

  const quit = () => void window.hermesDesktop?.updates?.quitApp?.()

  return (
    <div
      aria-modal="true"
      className="fixed inset-0 z-(--z-forced-update) grid place-items-center bg-(--ui-chat-surface-background) px-6"
      data-overlay-surface=""
      data-testid="mandatory-update-overlay"
      ref={containerRef}
      role="alertdialog"
      tabIndex={-1}
    >
      <div className="w-full max-w-md rounded-2xl border border-(--ui-stroke-secondary) bg-background/80 p-7 shadow-lg">
        <p className="text-xs font-medium tracking-wide text-(--ui-accent)">{PRODUCT_POLICY.productName}</p>

        <h1 className="mt-3 text-xl font-semibold text-foreground">
          {failed ? copy.failedTitle : staged ? copy.stagedTitle : copy.blockingTitle}
        </h1>

        <p className="mt-1 text-sm text-muted-foreground">
          {failed
            ? copy.failedHint
            : staged
              ? copy.stagedBody(blocking.pendingVersion ?? blocking.targetVersion ?? '')
              : copy.blockingBody}
        </p>

        <p className="mt-3 font-mono text-xs text-muted-foreground">
          {copy.versions(blocking.currentVersion ?? '—', blocking.targetVersion ?? '—')}
        </p>

        {blocking.releaseNotes && !installing && !failed && (
          <div className="mt-5">
            <p className="text-[0.625rem] font-semibold uppercase tracking-wide text-muted-foreground">
              {copy.releaseNotes}
            </p>
            <p className="mt-1.5 max-h-40 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-foreground">
              {blocking.releaseNotes}
            </p>
          </div>
        )}

        {installing && (
          <div className="mt-5 grid gap-2">
            <Progress
              aria-label={copy.installing}
              indeterminate={percent === null}
              size="lg"
              value={percent === null ? 0 : percent / 100}
            />
            {apply.message.trim() && (
              <p className="break-words text-xs leading-5 text-muted-foreground">{apply.message}</p>
            )}
            <p className="text-xs leading-5 text-muted-foreground">{copy.installingHint}</p>
          </div>
        )}

        {failed && apply.message.trim() && (
          <p className="mt-4 break-words text-xs leading-5 text-destructive">{apply.message}</p>
        )}

        <div className="mt-6 grid gap-2">
          <Button
            className="w-full"
            disabled={installing}
            onClick={() => void (staged ? installStagedUpdate() : applyUpdates())}
          >
            {installing && <Loader2 className="size-3.5 animate-spin" />}
            {installing ? copy.installing : failed ? copy.retry : staged ? copy.stagedInstallNow : copy.install}
          </Button>

          {/* 退出始终留着。安装期间退出也是安全的 —— 交接安装器之前旧版本一个
              字节都没被动过，而一个连出口都没有的界面比可以退出糟得多。 */}
          <Button className="w-full" onClick={quit} type="button" variant="text">
            {copy.quit}
          </Button>
        </div>
      </div>
    </div>
  )
}
