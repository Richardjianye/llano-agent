/**
 * 可选更新的模态提示（品牌版，CUSTOM）
 *
 * 和 `product-mandatory-update-overlay.tsx` 是一对：那个是强制更新，没有出口；
 * 这个是可选更新，带蒙版居中弹出，让人一定看得见，但可以「稍后提醒」（24 小时）。
 * 点外面不关 —— 一不小心点空白处就关掉，又回到了「很容易被忽略」。
 * 安装进度就在框里显示，不再跳到另一个窗口。
 */

import { useStore } from '@nanostores/react'
import { Dialog as DialogPrimitive } from 'radix-ui'

import { Button } from '@/components/ui/button'
import { Dialog, DialogOverlay, DialogPortal } from '@/components/ui/dialog'
import { Progress } from '@/components/ui/progress'
import { useI18n } from '@/i18n'
import { Loader2 } from '@/lib/icons'
import { PRODUCT_POLICY } from '@/lib/product-policy'
import { $mandatoryUpdate } from '@/store/product-mandatory-update'
import { $optionalUpdatePrompt, closeOptionalUpdatePrompt } from '@/store/product-optional-update'
import { $updateApply, $updateStatus, applyUpdates, snoozeUpdatePrompt } from '@/store/updates'

export function ProductOptionalUpdateDialog() {
  const { t } = useI18n()
  const copy = t.productUpdate
  const prompt = useStore($optionalUpdatePrompt)
  const mandatory = useStore($mandatoryUpdate)
  const apply = useStore($updateApply)
  const current = useStore($updateStatus)

  // 强制更新的全屏阻断在场时，它说了算
  const open = Boolean(prompt) && !mandatory

  if (!open || !prompt) {
    return null
  }

  const installing = apply.applying || apply.stage === 'restart'
  const failed = apply.stage === 'error'

  const percent =
    typeof apply.percent === 'number' && Number.isFinite(apply.percent)
      ? Math.max(2, Math.min(100, Math.round(apply.percent)))
      : null

  const later = () => {
    if (installing) {
      return
    }

    snoozeUpdatePrompt()
    closeOptionalUpdatePrompt()
  }

  const targetVersion = prompt.targetVersion ?? current?.targetVersion ?? ''

  return (
    <Dialog onOpenChange={next => void (next ? undefined : later())} open>
      <DialogPortal>
        <DialogOverlay className="bg-black/55 backdrop-blur-sm" data-testid="optional-update-backdrop" />
        <DialogPrimitive.Content
          aria-describedby="optional-update-body"
          className="fixed left-1/2 top-1/2 z-(--z-modal) w-[min(28rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-(--ui-stroke-secondary) bg-background p-7 shadow-2xl outline-none data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95"
          data-testid="optional-update-dialog"
          onInteractOutside={event => event.preventDefault()}
        >
          <p className="text-xs font-medium tracking-wide text-(--ui-accent)">{PRODUCT_POLICY.productName}</p>

          <DialogPrimitive.Title className="mt-3 text-xl font-semibold text-foreground">
            {failed ? copy.failedTitle : copy.optionalTitle(targetVersion)}
          </DialogPrimitive.Title>

          <p className="mt-1 text-sm text-muted-foreground" id="optional-update-body">
            {failed ? copy.failedHint : copy.optionalBody}
          </p>

          <p className="mt-3 font-mono text-xs text-muted-foreground">
            {copy.versions(prompt.currentVersion ?? current?.currentVersion ?? '—', targetVersion || '—')}
          </p>

          {prompt.releaseNotes && !installing && !failed && (
            <div className="mt-5">
              <p className="text-[0.625rem] font-semibold uppercase tracking-wide text-muted-foreground">
                {copy.releaseNotes}
              </p>
              <p className="mt-1.5 max-h-48 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-foreground">
                {prompt.releaseNotes}
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
            <Button autoFocus className="w-full" disabled={installing} onClick={() => void applyUpdates()}>
              {installing && <Loader2 className="size-3.5 animate-spin" />}
              {installing ? copy.installing : failed ? copy.retry : copy.install}
            </Button>
            <Button className="w-full" disabled={installing} onClick={later} type="button" variant="text">
              {copy.later}
            </Button>
          </div>
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  )
}
