import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { removeCollabMember } from '@/hermes'
import { useI18n } from '@/i18n'
import {
  $collabProjects,
  $collabState,
  claimCollabAndAsk,
  createCollabTaskFrom,
  inviteCollabPerson,
  loadCollabSidebars,
  pullMyAgentInto,
  searchCollabDirectory
} from '@/store/collab'
import type { CollabDirectoryUser, CollabMember, CollabTask } from '@/types/hermes'

import { AgentSettings } from './agent-settings'
import { TaskResult } from './task-result'

/**
 * 成员 / agent 列表与任务侧栏（阶段 27-D）。
 *
 * agent 是**一等成员**，不是人的一个属性 —— 「这条消息是谁说的」要答得清楚，
 * 所以列表里人和 agent 分开列，agent 一行上带着它的主人和触发范围。
 */

/**
 * 加成员 —— 从全员通讯录里挑人。
 *
 * 通讯录全员可见（与钉钉一致，由负责人拍板）。字段只有姓名与部门：服务端就只
 * 回这四个，见 `router/v1/directory.py`。
 *
 * 「只有负责人能加成员」的判定**在服务端**，这里不预判。预判的话就有两份判定，
 * 而它们会漂 —— 漂的方向通常是客户端先放行，然后用户收到一个莫名其妙的 403。
 */
function InviteMember({ code, existing }: { code: string; existing: Set<string> }) {
  const { t } = useI18n()
  const [query, setQuery] = useState('')
  const [people, setPeople] = useState<CollabDirectoryUser[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) {
      return
    }

    let cancelled = false

    // 输入停 250ms 再查。每敲一个字查一次，中文输入法一个词就是四五次请求。
    const timer = setTimeout(() => {
      void searchCollabDirectory(query).then(found => {
        if (!cancelled) {
          setPeople(found)
        }
      })
    }, 250)

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [open, query])

  if (!open) {
    return (
      <Button
        className="h-6 px-2 text-[11px]"
        data-testid="collab-invite-open"
        onClick={() => setOpen(true)}
        size="sm"
        variant="outline"
      >
        {t.collab.members.invite}
      </Button>
    )
  }

  return (
    <div className="space-y-1">
      <Input
        autoFocus
        className="h-6 text-[11px]"
        data-testid="collab-invite-search"
        onChange={event => setQuery(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Escape') {
            setOpen(false)
          }
        }}
        placeholder={t.collab.members.invitePlaceholder}
        value={query}
      />

      <ul className="max-h-32 overflow-y-auto">
        {people
          .filter(person => !existing.has(person.uid))
          .map(person => (
            <li className="flex items-center justify-between gap-2 py-0.5 text-[11px]" key={person.uid}>
              <span className="truncate">
                {person.realname || person.username}
                <span className="ml-1 text-(--ui-text-quaternary)">{person.dept_name}</span>
              </span>
              <button
                className="shrink-0 rounded border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-2 py-1 text-(--ui-text-tertiary) hover:bg-(--ui-bg-secondary) disabled:opacity-50"
                data-testid={`collab-invite-${person.uid}`}
                disabled={busy === person.uid}
                onClick={() => {
                  setBusy(person.uid)
                  setError('')
                  void inviteCollabPerson(code, person.uid, person.username)
                    .then(() => setOpen(false))
                    .catch(cause => setError(cause instanceof Error ? cause.message : String(cause)))
                    .finally(() => setBusy(''))
                }}
                type="button"
              >
                {t.collab.members.add}
              </button>
            </li>
          ))}

        {people.length === 0 ? (
          <li className="py-0.5 text-[11px] text-(--ui-text-quaternary)">{t.collab.members.noMatch}</li>
        ) : null}
      </ul>

      {error ? (
        <div className="text-[11px] text-destructive" data-testid="collab-invite-error">
          {error}
        </div>
      ) : null}
    </div>
  )
}

export function MembersPanel({ code }: { code: string }) {
  const { t } = useI18n()
  const state = useStore($collabState)[code]
  const members = state?.members ?? []
  const [error, setError] = useState('')
  const [pulling, setPulling] = useState(false)
  const [removing, setRemoving] = useState<number | null>(null)
  const project = useStore($collabProjects).find(p => p.code === code)
  const me = members.find(m => m.is_me && m.member_kind !== 'agent')
  const readOnly = project?.status === 'archived' || Boolean(state?.stoppedReason)
  const canManage = !readOnly && (project?.can_manage ?? me?.role === 'owner')
  const running = (state?.turns ?? []).find(turn => turn.status === 'running')
  const hopsExhausted = (state?.hops ?? 0) >= (state?.hopLimit ?? 8)
  const people = members.filter(member => member.member_kind !== 'agent')
  const agents = members.filter(member => member.member_kind === 'agent')

  const row = (member: CollabMember) => (
    <li className="flex items-center justify-between gap-2 py-0.5 text-[12px]" key={member.id}>
      <span className="truncate">
        {member.member_kind === 'agent' ? member.agent_label || '—' : member.username || member.user_uid}
      </span>

      {member.member_kind === 'agent' ? (
        <span className="text-xs text-(--ui-text-secondary)">
          {state?.availability?.find(a => a.member_id === member.id)?.status === 'online'
            ? t.collab.functional.online
            : member.respond_to === 'nobody'
              ? t.collab.functional.paused
              : t.collab.functional.offline}
        </span>
      ) : null}
      {member.member_kind === 'agent' && member.is_me ? (
        // 只有主人能改自己 agent 的触发范围 —— 这里也只给自己的那几行放选择器。
        // **判定仍在服务端**：这个下拉改的是配置，改不动任何一条实际权限，
        // 非法值网关会直接拒绝（不静默纠正）。
        <AgentSettings code={code} member={member} />
      ) : (
        <span className="shrink-0 text-[10px] text-(--ui-text-quaternary)">
          {member.member_kind === 'agent'
            ? t.collab.respondTo[member.respond_to || 'project_members'] || member.respond_to
            : member.role === 'viewer'
              ? t.collab.members.readonly
              : (member.role ?? '')}
        </span>
      )}
      {(canManage && !member.is_me) || (!readOnly && member.is_me && member.member_kind === 'agent') ? (
        <Button onClick={() => setRemoving(member.id)} size="sm" variant="secondary">
          {t.collab.functional.remove}
        </Button>
      ) : null}
    </li>
  )

  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-start gap-2">
        {canManage ? (
          <InviteMember
            code={code}
            existing={new Set(members.filter(m => m.member_kind !== 'agent').map(member => member.user_uid))}
          />
        ) : null}
        {!readOnly && me?.role !== 'viewer' && !agents.some(agent => agent.is_me) ? (
          <Button
            data-testid="collab-pull-my-agent"
            disabled={pulling}
            onClick={() => {
              setPulling(true)
              setError('')
              void pullMyAgentInto(code)
                .catch(cause => setError(cause instanceof Error ? cause.message : String(cause)))
                .finally(() => setPulling(false))
            }}
            size="sm"
            variant="outline"
          >
            {pulling ? t.collab.members.pulling : t.collab.members.pullMyAgent}
          </Button>
        ) : null}
      </div>
      <div className="text-[10px] text-(--ui-text-quaternary)">{t.collab.members.people}</div>
      <ul>{people.map(row)}</ul>
      {agents.length > 0 ? (
        <>
          <div className="text-[10px] text-(--ui-text-quaternary)">{t.collab.members.agents}</div>
          <ul>{agents.map(row)}</ul>
        </>
      ) : null}

      {removing !== null ? (
        <div className="space-y-1" role="alert">
          <p>
            {t.collab.functional.remove}：
            {members.find(m => m.id === removing)?.agent_label || members.find(m => m.id === removing)?.username}
          </p>
          <Button
            disabled={pulling}
            onClick={() => {
              setPulling(true)
              setError('')
              void removeCollabMember(code, removing)
                .then(async () => {
                  setRemoving(null)
                  await loadCollabSidebars(code)
                })
                .catch(e => setError(String(e.message || e)))
                .finally(() => setPulling(false))
            }}
            size="sm"
            variant="destructive"
          >
            {t.collab.functional.confirm}
          </Button>
          <Button onClick={() => setRemoving(null)} size="sm" variant="secondary">
            {t.collab.functional.cancel}
          </Button>
        </div>
      ) : null}

      {/*
        把自己的 agent 拉进来。**只能拉自己的** —— 服务端在 agent 分支一律用调用者
        本人的 uid，拉别人的 agent 等于替别人的凭据做决定。

        没有这个按钮的话，桌面端建出来的项目里一个 agent 都没有，而 agent 在群里
        说话正是这个功能存在的理由（2026-09-07 之前这一步只有后台有）。
      */}

      {running ? (
        <div className="text-[10px] text-(--ui-text-tertiary)">{t.collab.turns.running(running.agent_label || '')}</div>
      ) : null}
      {hopsExhausted ? (
        // 熔断是运行时的事实，不是错误。措辞照服务端那句：说停了，也说怎么继续。
        <div className="text-[10px] text-amber-600 dark:text-amber-400">{t.collab.turns.hopLimit}</div>
      ) : null}
      {error ? <div className="text-[11px] text-destructive">{error}</div> : null}
    </section>
  )
}

export function TasksPanel({ code }: { code: string }) {
  const { t } = useI18n()
  const state = useStore($collabState)[code]
  const tasks = state?.tasks ?? []
  const project = useStore($collabProjects).find(p => p.code === code)
  const me = state?.members.find(m => m.is_me && m.member_kind !== 'agent')
  const readOnly = project?.status === 'archived' || Boolean(state?.stoppedReason) || me?.role === 'viewer'
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const add = async () => {
    const clean = title.trim()

    if (!clean || busy || readOnly) {
      return
    }

    setBusy(true)
    setError('')

    try {
      // 手写的任务不带 source_message_id，服务端据此定 untrusted —— 客户端不传
      // 那个标记（传得了的话，注入一句就绕过整条 P0-6 防线）。
      await createCollabTaskFrom(code, clean)
      setTitle('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const claim = async (task: CollabTask) => {
    setError('')

    try {
      // 认领**只到 claimed**。确认框会停下来等人读完原文（P0-6）。
      await claimCollabAndAsk(code, task.id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  return (
    <section className="space-y-2">
      <div className="text-[11px] font-medium text-(--ui-text-tertiary)">{t.collab.tasks.title}</div>

      <div className="flex gap-1">
        <Input
          className="h-7 text-[12px]"
          onChange={event => setTitle(event.target.value)}
          placeholder={t.collab.tasks.newTitle}
          value={title}
        />
        <Button
          className="h-7"
          disabled={readOnly || busy || title.trim().length === 0}
          onClick={() => void add()}
          size="sm"
        >
          {t.collab.tasks.create}
        </Button>
      </div>

      {tasks.length === 0 ? (
        <div className="text-[11px] text-(--ui-text-quaternary)">{t.collab.tasks.empty}</div>
      ) : null}

      <ul className="space-y-1.5">
        {tasks.map(task => (
          <li className="rounded border border-(--ui-stroke-secondary) px-2 py-1.5" key={task.id}>
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-[12px]">
                #{task.seq} {task.title}
              </span>
              {!readOnly &&
              (task.status === 'open' ||
                (task.claimed_by_uid === me?.user_uid && ['claimed', 'active'].includes(task.status))) ? (
                <Button className="h-6" onClick={() => void claim(task)} size="sm" variant="secondary">
                  {task.status === 'open' ? t.collab.tasks.claim : t.collab.functional.continueConfirm}
                </Button>
              ) : (
                <span className="shrink-0 text-[10px] text-(--ui-text-quaternary)">
                  {task.status === 'claimed'
                    ? t.collab.tasks.claimed
                    : task.status === 'active'
                      ? t.collab.tasks.active
                      : task.status === 'submitted'
                        ? t.collab.functional.submitted
                        : task.status === 'done'
                          ? t.collab.functional.done
                          : task.status}
                </span>
              )}
            </div>
            {task.claimed_by_username ? (
              <p className="text-xs text-(--ui-text-secondary)">{task.claimed_by_username}</p>
            ) : null}
            {!state?.stoppedReason ? (
              <TaskResult
                canAccept={Boolean(
                  project?.can_manage || me?.role === 'owner' || (me && task.created_by_uid === me.user_uid)
                )}
                code={code}
                mine={task.claimed_by_uid === me?.user_uid}
                readOnly={readOnly}
                task={task}
              />
            ) : null}
            {task.untrusted ? (
              // 不可信标记要一直看得见：它会跟着任务描述进入私聊上下文。
              <div className="text-[10px] text-amber-600 dark:text-amber-400">{t.collab.tasks.untrusted}</div>
            ) : null}
            {task.source_message_id ? (
              <div className="text-[10px] text-(--ui-text-quaternary)">{t.collab.tasks.fromMessage}</div>
            ) : null}
          </li>
        ))}
      </ul>

      {error ? <div className="text-[11px] text-destructive">{error}</div> : null}
    </section>
  )
}
