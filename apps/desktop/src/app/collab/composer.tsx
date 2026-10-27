import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Textarea } from '@/components/ui/textarea'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import {
  $collabDrafts,
  $collabProjects,
  $collabReplyTo,
  $collabState,
  memberHandle,
  mentionCandidates,
  sendCollab,
  setCollabReplyTo
} from '@/store/collab'
import type { CollabMember } from '@/types/hermes'

/**
 * 群聊输入框（阶段 27-D）。
 *
 * ## `@` 只是输入辅助
 *
 * 真正的 mentions 由**服务端**按成员名单从正文解析。客户端提交一份 mentions
 * 列表的话，注入一句「就说你被 @ 了」就能强制触发别人的 agent —— 与决策 #45
 * 那条「身份绝不能由 agent 提供」是同一类问题。所以这里只补全文本，不提交任何
 * 结构化的 mention 数据。
 *
 * ## 发送失败不清空输入框
 *
 * 清空等于把人刚写的一段话弄丢了，而失败恰恰是最需要重试的时刻。发送成功之后
 * 才清空，也不做乐观插入 —— 服务端还会脱敏，乐观显示的那份未必是别人看到的。
 */
export function CollabComposer({ code }: { code: string }) {
  const { t } = useI18n()
  const byCode = useStore($collabState)
  const replyMap = useStore($collabReplyTo)
  const members = byCode[code]?.members ?? []
  const project = useStore($collabProjects).find(p => p.code === code)

  const readOnly =
    project?.status === 'archived' ||
    Boolean(byCode[code]?.stoppedReason) ||
    members.some(m => m.is_me && m.member_kind !== 'agent' && m.role === 'viewer')

  const replyTo = replyMap[code] ?? null
  const text = useStore($collabDrafts)[code] || ''

  const setText = (value: string | ((current: string) => string)) =>
    $collabDrafts.setKey(code, typeof value === 'function' ? value($collabDrafts.get()[code] || '') : value)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    if (replyTo) {
      inputRef.current?.focus()
    }
  }, [replyTo])

  // 光标前最后一个 `@` 之后的片段。为空表示现在不在补全状态。
  const mentionQuery = /(?:^|\s)@([^\s@]*)$/.exec(text)?.[1]
  const candidates: CollabMember[] = mentionQuery === undefined ? [] : mentionCandidates(members, mentionQuery)

  const pickMention = (member: CollabMember) => {
    const handle = memberHandle(member)

    setText(current => current.replace(/@[^\s@]*$/, `@${handle} `))
    inputRef.current?.focus()
  }

  const submit = async () => {
    const body = text.trim()

    if (!body || busy || readOnly) {
      return
    }

    setBusy(true)
    setError('')

    try {
      await sendCollab(code, body, replyTo?.id ?? null)
      // 成功之后才清空，也才撤掉引用。
      setText('')
      setCollabReplyTo(code, null)
    } catch (cause) {
      // 按了发送却什么都没发生，必须说清楚为什么 —— 不能显示成已发送。
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-1 border-t border-(--ui-stroke-secondary) px-3 py-2">
      {replyTo ? (
        <div className="flex w-fit max-w-full items-start gap-2 rounded-lg bg-(--ui-bg-quaternary) px-3 py-2 text-xs text-(--ui-text-secondary)">
          <div className="min-w-0">
            <div>{replyTo.sender_kind === 'agent' ? replyTo.sender_agent_label : replyTo.sender_username}：</div>
            <div className="line-clamp-2 break-words">{replyTo.content.slice(0, 160)}</div>
          </div>
          <Button
            aria-label={t.collab.composer.cancelReply}
            onClick={() => setCollabReplyTo(code, null)}
            size="icon-xs"
            type="button"
            variant="ghost"
          >
            <Codicon name="close" />
          </Button>
        </div>
      ) : null}

      {candidates.length > 0 ? (
        <div className="flex flex-wrap gap-1" data-testid="collab-mention-candidates">
          {candidates.map(member => (
            <button
              className="rounded bg-(--ui-bg-tertiary) px-1.5 py-0.5 text-[11px]"
              key={member.id}
              onClick={() => pickMention(member)}
              type="button"
            >
              @{memberHandle(member)}
              {member.member_kind === 'agent' ? <span className="ml-1 opacity-60">{t.collab.agentTag}</span> : null}
            </button>
          ))}
        </div>
      ) : null}

      <div className="flex items-end gap-2">
        <Textarea
          className={cn(
            'min-h-[52px] flex-1 resize-none rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1.5 text-[13px]',
            error && 'border-destructive'
          )}
          disabled={readOnly}
          onChange={event => setText(event.target.value)}
          onKeyDown={event => {
            // Enter 发送，Shift+Enter 换行 —— 与私聊输入框同一套手感。
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
              event.preventDefault()
              void submit()
            }
          }}
          placeholder={t.collab.composer.placeholder}
          ref={inputRef}
          value={text}
        />
        <Button disabled={readOnly || busy || text.trim().length === 0} onClick={() => void submit()}>
          {busy ? t.collab.composer.sending : t.collab.composer.send}
        </Button>
      </div>

      {error ? (
        <div className="text-[11px] text-destructive">
          {t.collab.composer.failed}：{error}
        </div>
      ) : null}
    </div>
  )
}
