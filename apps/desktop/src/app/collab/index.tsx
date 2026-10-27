import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { Fragment, useEffect, useRef, useState } from 'react'

import { CompactMarkdown } from '@/components/chat/compact-markdown'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { CopyButton } from '@/components/ui/copy-button'
import { Input } from '@/components/ui/input'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import {
  $collabAuthenticated,
  $collabProjects,
  $collabProjectsLoaded,
  $collabProjectsStale,
  $collabSelected,
  $collabState,
  $collabUnread,
  $collabVisible,
  createCollabProjectAndOpen,
  createCollabTaskFrom,
  selectCollabProject,
  setCollabReplyTo,
  startCollabProjectsLoad,
  stopCollabPolling,
  stopCollabTicking
} from '@/store/collab'
import { $collabReceiving, $collabRuntimeActive } from '@/store/collab-runtime'
import type { CollabMessage } from '@/types/hermes'

import { ListColumn } from '../master-detail'
import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'

import { CollabComposer } from './composer'
import { ProjectManagement } from './management'
import { MembersPanel, TasksPanel } from './panels'
import { ReplyStatus } from './reply-status'
import { TaskConfirmDialog } from './task-confirm-dialog'
import { useTaskExecution } from './task-execution'

/**
 * 项目协作页（阶段 27-C）—— 「消息平台」下方的**独立一级页面**。
 *
 * 它与私聊明确分开：独立 route、独立 store、独立恢复水位。项目消息不写入私聊
 * session 列表，也不混排进 Recents。两边共享的只有纯展示组件。
 *
 * 这一刀只铺页面骨架与数据通路：项目列表、未读、消息流、离线与停止状态。
 * 发送、引用、`@` 自动完成、成员/agent 列表、任务侧栏与消息转任务是 27-D。
 */

interface CollabViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
}

function SenderLabel({ message }: { message: CollabMessage }) {
  const { t } = useI18n()
  // agent 成员显示 agent 名，人显示用户名。两个都空时退到 uid ——
  // **不编一个像样的假名字**，否则分不清是数据没配还是本来就没有。
  const name = message.sender_agent_label || message.sender_username || message.sender_uid || '—'

  return (
    <span className="flex items-center gap-1 text-[11px] text-(--ui-text-tertiary)">
      {name}
      {message.sender_kind === 'agent' ? (
        // agent 的发言必须一眼能认出来。看不出是人是 agent 的群聊，
        // 「这句话算谁说的」就只能靠猜。
        <span className="rounded bg-(--ui-bg-tertiary) px-1 py-px text-[10px] uppercase">{t.collab.agentTag}</span>
      ) : null}
    </span>
  )
}

/**
 * 新建项目。
 *
 * 建项目原本只在网关后台的「协作」页 —— 而后台的定位是看板。协作发生在客户端，
 * 建项目也该在客户端（2026-09-07）。
 *
 * 表单**不抄服务端那套 code 规则**（小写、不可改）：抄一份到这里，两份迟早会漂，
 * 而漂的那天用户看到的是「客户端说可以，服务端说不行」。这里只挡空值，其余由
 * 服务端判，判出来的话**原样显示** —— 写失败绝不能表现成建好了。
 */
function CreateProjectForm({ onDone }: { onDone: () => void }) {
  const { t } = useI18n()
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    const trimmed = code.trim()

    if (!trimmed || busy) {
      return
    }

    setBusy(true)
    setError('')

    try {
      await createCollabProjectAndOpen(trimmed, name.trim())
      onDone()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-1.5 px-2 py-2">
      <input
        autoFocus
        className="w-full rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-[12px]"
        data-testid="collab-new-code"
        disabled={busy}
        onChange={event => setCode(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Enter') {
            void submit()
          } else if (event.key === 'Escape') {
            onDone()
          }
        }}
        placeholder={t.collab.create.codePlaceholder}
        value={code}
      />
      <input
        className="w-full rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-[12px]"
        data-testid="collab-new-name"
        disabled={busy}
        onChange={event => setName(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Enter') {
            void submit()
          } else if (event.key === 'Escape') {
            onDone()
          }
        }}
        placeholder={t.collab.create.namePlaceholder}
        value={name}
      />
      <div className="text-[10px] text-(--ui-text-quaternary)">{t.collab.create.codeHint}</div>
      {error ? (
        <div className="text-[11px] text-red-600 dark:text-red-400" data-testid="collab-new-error">
          {error}
        </div>
      ) : null}
      <div className="flex gap-2">
        <button
          className="rounded bg-(--ui-bg-tertiary) px-2 py-1 text-[11px] disabled:opacity-50"
          data-testid="collab-new-submit"
          disabled={busy || !code.trim()}
          onClick={() => void submit()}
          type="button"
        >
          {busy ? t.collab.create.creating : t.collab.create.submit}
        </button>
        <button
          className="rounded border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-2 py-1 text-[11px] text-(--ui-text-tertiary)"
          disabled={busy}
          onClick={onDone}
          type="button"
        >
          {t.collab.create.cancel}
        </button>
      </div>
    </div>
  )
}

export function CollabView({ setStatusbarItemGroup: _setStatusbarItemGroup, ...props }: CollabViewProps) {
  const { t } = useI18n()
  const executeTask = useTaskExecution()
  const receiving = useStore($collabReceiving)
  const projects = useStore($collabProjects)
  const stale = useStore($collabProjectsStale)
  const loaded = useStore($collabProjectsLoaded)
  const authenticated = useStore($collabAuthenticated)
  const selected = useStore($collabSelected)
  const byCode = useStore($collabState)
  const unread = useStore($collabUnread)
  const state = selected ? byCode[selected] : undefined
  const [creating, setCreating] = useState(false)
  const [query, setQuery] = useState('')
  const [archived, setArchived] = useState(false)
  const myUid = state?.members.find(m => m.is_me && m.member_kind !== 'agent')?.user_uid
  const project = projects.find(p => p.code === selected)
  const [panel, setPanel] = useState<'members' | 'tasks' | null>(null)
  const [projectsOpen, setProjectsOpen] = useState(false)
  const [showLatest, setShowLatest] = useState(false)
  const [actionError, setActionError] = useState('')

  const writable =
    project?.status !== 'archived' &&
    !state?.stoppedReason &&
    state?.members.some(m => m.is_me && m.member_kind !== 'agent' && m.role !== 'viewer')

  const scrollRef = useRef<HTMLDivElement>(null)
  const stickToBottom = useRef(true)
  // Page-local scroll intent is reset on project changes; it does not mirror
  // reactive store state into a ref.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    stickToBottom.current = true
    setActionError('')

    if (selected) {
      $collabUnread.setKey(selected, 0)
    }
  }, [selected])
  useEffect(() => {
    if (stickToBottom.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [selected, state?.messages, state?.turns, state?.pending])

  useEffect(() => {
    // 重试到读出来为止：sidecar 是被主进程拉起来的，这一页完全可能比它先到，
    // 而只读一次的话那一次失败就永久停在「取不到」（2026-09-07 真机上的表现）。
    $collabVisible.set(true)
    const stopLoading = $collabRuntimeActive.get() ? () => {} : startCollabProjectsLoad()

    // Page-local fallback stops here. The authenticated app runtime continues
    // receiving for joined projects and owns logout / revocation cleanup.
    return () => {
      $collabVisible.set(false)
      stopLoading()
      stopCollabPolling()
      stopCollabTicking()
    }
  }, [])

  const stoppedNotice = state?.stoppedReason
    ? (t.collab.stopped as Record<string, string>)[state.stoppedReason] || t.collab.stopped.stopped
    : ''

  const projectRail = (
    <div className="h-full [&>aside]:h-full">
      <ListColumn
        header={
          <div className="grid grid-cols-[1fr_auto] items-center gap-x-2 gap-y-1 px-2 pb-2">
            <div className="text-[13px] font-medium">{t.collab.title}</div>
            {authenticated ? (
              <Tip label={t.collab.create.submit}>
                <Button
                  aria-label={t.collab.create.submit}
                  data-testid="collab-new-project"
                  onClick={() => setCreating(value => !value)}
                  size="icon-xs"
                  type="button"
                  variant="ghost"
                >
                  <Codicon name="add" />
                </Button>
              </Tip>
            ) : null}
            <div className="col-span-2 text-[11px] text-(--ui-text-tertiary)">{t.collab.subtitle}</div>
          </div>
        }
      >
        <div className="space-y-3 px-1 pb-3">
          <Button onClick={() => $collabReceiving.set(!receiving)} size="sm" variant="secondary">
            {receiving ? t.collab.functional.pause : t.collab.functional.resume}
          </Button>
          {creating ? <CreateProjectForm onDone={() => setCreating(false)} /> : null}
          {!authenticated ? (
            <div className="px-2 py-3 text-[12px] text-(--ui-text-tertiary)">{t.collab.signedOut}</div>
          ) : null}

          {/*
            「一个项目都没有」只有在**真的读到过**列表之后才说得出口。读失败时说
            这句话，是在拿一个我们没有的数据下断言 —— 而它长得和真实的空列表
            一模一样，人分不出来。
          */}
          {authenticated && loaded && projects.length === 0 ? (
            <div className="space-y-1 px-2 py-3">
              <div className="text-[12px] text-(--ui-text-tertiary)">{t.collab.empty}</div>
              <div className="text-[11px] text-(--ui-text-quaternary)">{t.collab.emptyHint}</div>
            </div>
          ) : null}

          {authenticated && !loaded ? (
            <div className="px-2 py-3 text-[12px] text-(--ui-text-tertiary)" data-testid="collab-projects-unavailable">
              {stale ? t.collab.listUnavailable : t.collab.loading}
            </div>
          ) : null}

          <Input
            aria-label={t.collab.functional.search}
            onChange={e => setQuery(e.target.value)}
            placeholder={t.collab.functional.search}
            value={query}
          />
          <label className="flex items-center gap-2">
            <input checked={archived} onChange={e => setArchived(e.target.checked)} type="checkbox" />
            {t.collab.functional.showArchived}
          </label>
          <div className="space-y-0.5">
            {projects
              .filter(
                p =>
                  (archived || p.status !== 'archived') &&
                  `${p.name || ''} ${p.code}`.toLowerCase().includes(query.toLowerCase())
              )
              .map(project => (
                <button
                  aria-current={selected === project.code ? 'page' : undefined}
                  className={cn(
                    'flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-[12px] transition-colors',
                    selected === project.code ? 'bg-(--ui-bg-tertiary)' : 'hover:bg-(--ui-bg-secondary)'
                  )}
                  key={project.code}
                  onClick={() => {
                    selectCollabProject(project.code)
                    setProjectsOpen(false)
                  }}
                  type="button"
                >
                  <span className="truncate">{project.name || project.code}</span>
                  {(unread[project.code] ?? 0) > 0 ? (
                    // 项目未读是**项目级**的，与会话未读没有任何共用状态。
                    <span className="rounded-full bg-primary/10 px-1.5 text-[10px] text-primary">
                      {unread[project.code]}
                    </span>
                  ) : null}
                </button>
              ))}
          </div>

          {stale && loaded ? (
            <div className="px-2 py-2 text-[11px] text-(--ui-text-quaternary)">{t.collab.offline}</div>
          ) : null}
        </div>
      </ListColumn>
    </div>
  )

  return (
    <section className="@container flex h-full min-h-0 flex-col bg-(--ui-chat-surface-background)" {...props}>
      <div className="flex min-h-0 flex-1">
        <aside className="hidden h-full w-56 shrink-0 border-r border-(--ui-stroke-tertiary) bg-(--ui-bg-sidebar) @2xl:block">
          {projectRail}
        </aside>
        <main className="relative flex min-w-0 flex-1 flex-col">
          <header className="flex min-h-12 shrink-0 flex-wrap items-center gap-2 border-b border-(--ui-stroke-tertiary) px-4 py-2">
            <Button className="@2xl:hidden" onClick={() => setProjectsOpen(true)} size="sm" variant="secondary">
              {t.collab.title}
            </Button>
            <strong className="min-w-0 flex-1 truncate text-sm">
              {project?.name || project?.code || t.collab.title}
            </strong>
            {project ? (
              <>
                <Button onClick={() => setPanel('members')} size="sm" variant="secondary">
                  {t.collab.members.title}
                </Button>
                <Button onClick={() => setPanel('tasks')} size="sm" variant="secondary">
                  {t.collab.tasks.title}
                </Button>
                <ProjectManagement key={project.code} project={project} />
              </>
            ) : null}
          </header>
          <div
            className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
            onScroll={() => {
              const node = scrollRef.current

              if (!node) {
                return
              }

              stickToBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 96
              setShowLatest(!stickToBottom.current)
            }}
            ref={scrollRef}
          >
            <div className="mx-auto max-w-3xl space-y-6 px-5 py-5">
              {!selected ? <div className="text-[12px] text-(--ui-text-tertiary)">{t.collab.pickProject}</div> : null}

              {selected && stoppedNotice ? (
                <div className="flex items-center gap-2 rounded border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[12px] text-amber-700 dark:text-amber-300">
                  <Codicon name="warning" />
                  {stoppedNotice}
                </div>
              ) : null}

              {actionError ? (
                <p className="text-destructive" role="alert">
                  {actionError}
                </p>
              ) : null}
              {state?.agentError ? (
                <p className="text-destructive" role="alert">
                  {t.collab.functional.failed}：{state.agentError}
                </p>
              ) : null}
              {selected && state?.stale && !stoppedNotice ? (
                <div className="rounded border border-(--ui-stroke-secondary) px-3 py-2 text-[11px] text-(--ui-text-tertiary)">
                  {t.collab.offline}
                </div>
              ) : null}

              {selected && state && state.messages.length === 0 && !state.loading ? (
                <div className="text-[12px] text-(--ui-text-tertiary)">{t.collab.noMessages}</div>
              ) : null}

              {selected
                ? (state?.messages ?? []).map(message => (
                    <Fragment key={message.seq}>
                      <article
                        className={cn(
                          'group min-w-0 space-y-1.5',
                          message.sender_kind === 'user' && message.sender_uid === myUid && 'flex flex-col items-end'
                        )}
                        id={`collab-message-${message.id}`}
                      >
                        <div className="flex items-center justify-between gap-3">
                          <SenderLabel message={message} />
                        </div>
                        <div className="w-fit max-w-full space-y-2 rounded-xl border border-(--ui-stroke-tertiary) bg-(--ui-bg-editor) px-3 py-2">
                          {message.reply_to_id ? (
                            <blockquote className="border-l-2 border-(--ui-stroke-secondary) pl-3 text-xs text-(--ui-text-secondary)">
                              {state?.messages.find(m => m.id === message.reply_to_id)?.content ||
                                t.collab.composer.replyingTo}
                            </blockquote>
                          ) : null}
                          <CompactMarkdown className="text-sm text-(--ui-text-primary)" text={message.content} />
                          {(message.redaction_hits ?? 0) > 0 ? (
                            // 收到的人有权知道自己看到的不是原话（P0-2）。
                            <div className="text-[10px] text-(--ui-text-quaternary)">{t.collab.redacted}</div>
                          ) : null}
                        </div>
                        <div className="flex items-center justify-end gap-1 whitespace-nowrap text-(--ui-text-tertiary)">
                          {message.created_at ? (
                            <time className="text-xs text-(--ui-text-tertiary)" dateTime={message.created_at}>
                              {new Date(message.created_at).toLocaleTimeString([], {
                                hour: '2-digit',
                                minute: '2-digit'
                              })}
                            </time>
                          ) : null}
                          <Tip label={t.collab.actions.reply}>
                            <Button
                              aria-label={t.collab.actions.reply}
                              disabled={!writable}
                              onClick={() => setCollabReplyTo(selected, message)}
                              size="icon-xs"
                              type="button"
                              variant="ghost"
                            >
                              <Codicon name="reply" />
                            </Button>
                          </Tip>
                          <Tip label={t.collab.actions.toTask}>
                            <Button
                              aria-label={t.collab.actions.toTask}
                              disabled={!writable}
                              onClick={() => {
                                // 消息转任务：正文原样带过去，来源也带过去。`untrusted`
                                // 由服务端按来源自己定 —— 客户端连这个字段都不该有。
                                void createCollabTaskFrom(
                                  selected,
                                  message.content.slice(0, 60),
                                  message.content,
                                  message.id
                                ).catch(error => setActionError(String(error)))
                              }}
                              size="icon-xs"
                              type="button"
                              variant="ghost"
                            >
                              <Codicon name="tasklist" />
                            </Button>
                          </Tip>
                          <CopyButton
                            appearance="icon"
                            buttonSize="icon-xs"
                            buttonVariant="ghost"
                            text={message.content}
                          />
                        </div>
                      </article>
                      <ReplyStatus code={selected} seq={message.seq} />
                    </Fragment>
                  ))
                : null}
              {selected ? <ReplyStatus code={selected} /> : null}
            </div>
          </div>
          {showLatest ? (
            <Button
              className="absolute right-5 bottom-28 shadow-sm"
              onClick={() => {
                stickToBottom.current = true
                scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
              }}
              size="sm"
              variant="secondary"
            >
              {t.collab.functional.latestMessages}
            </Button>
          ) : null}
          {selected ? (
            <div className="mx-auto w-full max-w-3xl shrink-0">
              <CollabComposer code={selected} key={selected} />
            </div>
          ) : null}
        </main>
      </div>
      <Sheet onOpenChange={setProjectsOpen} open={projectsOpen}>
        <SheetContent className="inset-y-auto top-8 bottom-0 h-auto w-72 p-0 pt-8" side="left">
          <SheetHeader className="sr-only">
            <SheetTitle>{t.collab.title}</SheetTitle>
          </SheetHeader>
          <div className="min-h-0 flex-1">{projectRail}</div>
        </SheetContent>
      </Sheet>
      <Sheet
        onOpenChange={open => {
          if (!open) {
            setPanel(null)
          }
        }}
        open={panel !== null}
      >
        <SheetContent className="inset-y-auto top-8 bottom-0 h-auto overflow-y-auto">
          <SheetHeader>
            <SheetTitle>{panel === 'members' ? t.collab.members.title : t.collab.tasks.title}</SheetTitle>
          </SheetHeader>
          <div className="p-4">
            {selected &&
              (panel === 'members' ? (
                <MembersPanel code={selected} key={selected} />
              ) : (
                <TasksPanel code={selected} key={selected} />
              ))}
          </div>
        </SheetContent>
      </Sheet>
      <TaskConfirmDialog onExecute={executeTask} />
    </section>
  )
}
