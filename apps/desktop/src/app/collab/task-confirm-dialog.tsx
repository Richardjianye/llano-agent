import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useI18n } from '@/i18n'
import { $collabPendingConfirm, confirmCollabStart, dismissCollabConfirm } from '@/store/collab'

/**
 * 开工确认框 —— P0-6 那道人在环闸门的落点（阶段 27-D）。
 *
 * ## 这个框的每一处都是防线的一部分
 *
 * - **原文完整、原样、不折叠、等宽**：摘要会把注入的那句话摘掉，而它恰恰是要
 *   让人看见的那一句；折叠等于默认没人看；等宽是为了让 `ignore previous
 *   instructions` 这类东西不被排版糊掉。
 * - **两个按钮都说人话**：「我已读过原文，确认开工」不是「确定」。人对「确定」
 *   的点击是肌肉记忆，对一句完整的承诺不是。
 * - **不做自动滚动到底、不做倒计时解锁**：那些机制训练的是「等一会儿再点」，
 *   不是「读一遍」。
 *
 * 认领与确认之间隔着这一步，是因为群里发一个夹带注入的任务描述、诱导认领、
 * 回私聊执行，是决策 #45 挡不住的那条绕道 —— 执行不发生在群聊里。
 */
export function TaskConfirmDialog({
  onExecute
}: {
  onExecute?: (pending: NonNullable<ReturnType<typeof $collabPendingConfirm.get>>) => Promise<void>
}) {
  const { t } = useI18n()
  const pending = useStore($collabPendingConfirm)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  if (!pending) {
    return null
  }

  const { confirmation } = pending

  const start = async () => {
    setBusy(true)
    setError('')

    try {
      if (onExecute) {
        await onExecute(pending)
      } else {
        await confirmCollabStart(pending.code, pending.taskId)
      }
    } catch (cause) {
      // 确认失败必须说清楚：这里静默失败的话，人以为已经开工了。
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog onOpenChange={open => !open && !busy && dismissCollabConfirm()} open>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t.collab.confirm.title}</DialogTitle>
        </DialogHeader>

        {confirmation.untrusted ? (
          // 措辞刻意不说「这可能是攻击」：绝大多数任务都是正常的，
          // 狼来了喊多了就没人看了。说的是事实 —— 这段文字不是你写的。
          <p className="text-[12px] text-(--ui-text-tertiary)">{confirmation.reminder || t.collab.confirm.lead}</p>
        ) : null}

        <div className="space-y-2">
          <div className="text-[13px] font-medium">{confirmation.title}</div>
          {/* 原文。不摘要、不高亮、不折叠、不截断。 */}
          <pre
            className="max-h-[45vh] overflow-auto rounded border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-3 font-mono text-[12px] whitespace-pre-wrap"
            data-testid="collab-task-original"
          >
            {confirmation.body}
          </pre>
        </div>

        {error ? (
          <div className="text-[12px] text-destructive">
            {t.collab.confirm.failed}：{error}
          </div>
        ) : null}

        <DialogFooter>
          <Button data-testid="collab-confirm-later" disabled={busy} onClick={dismissCollabConfirm} variant="secondary">
            {t.collab.confirm.later}
          </Button>
          <Button data-testid="collab-confirm-start" disabled={busy} onClick={() => void start()}>
            {t.collab.confirm.start}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
