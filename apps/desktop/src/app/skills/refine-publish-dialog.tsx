import { useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  checkSkillPublish,
  publishRefinedSkill,
  refineSkillDraft,
  type SkillPublishCheck,
  type SkillPublishResult,
  type SkillRefineFinding
} from '@/hermes'
import { useI18n } from '@/i18n'
import { Check, Loader2, RefreshCw } from '@/lib/icons'
import { invalidateSlashCompletions } from '@/lib/slash-completion-cache'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'

const SLUG = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/

/** 还没处理的敏感项：SKILL.md 里的要么已从整理稿里替换掉、要么确认保留；其它文件里的只能确认保留。 */
export function unresolvedFindings(findings: SkillRefineFinding[], draft: string, kept: ReadonlySet<string>) {
  return findings.filter(
    finding => !kept.has(finding.text) && (finding.file !== 'SKILL.md' || draft.includes(finding.text))
  )
}

/** 从 active 变成 true 起经过的整秒数；变回 false 归零。 */
function useElapsedSeconds(active: boolean) {
  const [seconds, setSeconds] = useState(0)

  useEffect(() => {
    setSeconds(0)

    if (!active) {
      return
    }

    const started = Date.now()
    const timer = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000)

    return () => window.clearInterval(timer)
  }, [active])

  return seconds
}

const timeOf = (epochSeconds: number) =>
  new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

interface RefinePublishDialogProps {
  displayName: string
  onClose: () => void
  skillName: string
}

/**
 * CUSTOM（2026-10-16，P2）：个人技能「整理并发布」。
 *
 * 两步，逐步显示进度：先机器检查（几秒，不调 AI），再 AI 整理（几十秒）。技能已符合发布规范
 * 时跳过 AI。两步的结果后端按技能内容缓存 —— 技能没改动就直接复用，只有员工点「重新检查」
 * 或技能变了才会重跑。然后让员工对照原文审阅整理稿，逐条处理敏感信息（替换成占位符或确认
 * 保留）—— 全部处理完才能发布；服务端还会再查一遍。默认用整理版替换本机这个技能，原版先备份。
 */
export function RefinePublishDialog({ displayName, onClose, skillName }: RefinePublishDialogProps) {
  const { t } = useI18n()
  const r = t.skills.refine
  // 第几次「重新检查」；0 是打开时，可以沿用缓存。
  const [generation, setGeneration] = useState(0)
  const [forceAi, setForceAi] = useState(false)
  const refresh = generation > 0

  const check = useQuery({
    queryKey: ['skill-publish-check', skillName, generation],
    queryFn: () => checkSkillPublish(skillName, refresh),
    gcTime: 0,
    retry: false,
    staleTime: Infinity
  })

  const checked = check.data
  const delisted = checked?.status.status === 'delisted'
  const skipAi = Boolean(checked && (delisted || (checked.conforms && !forceAi)))

  const refine = useQuery({
    queryKey: ['skill-publish-refine', skillName, generation, forceAi],
    queryFn: () => refineSkillDraft(skillName, refresh),
    enabled: Boolean(checked) && !skipAi,
    gcTime: 0,
    retry: false,
    staleTime: Infinity
  })

  const checkSeconds = useElapsedSeconds(check.isFetching)
  const refineSeconds = useElapsedSeconds(refine.isFetching)
  const ready = Boolean(checked) && (skipAi || Boolean(refine.data))
  const busy = check.isFetching || refine.isFetching

  const recheck = () => {
    setForceAi(false)
    setGeneration(value => value + 1)
  }

  const refineState: StepState = !checked
    ? 'waiting'
    : skipAi
      ? 'skipped'
      : refine.isFetching
        ? 'running'
        : refine.isError
          ? 'failed'
          : refine.data
            ? 'done'
            : 'waiting'

  return (
    <Dialog onOpenChange={open => !open && onClose()} open>
      <DialogContent bodyClassName="overflow-hidden" className="max-h-[88vh] max-w-5xl">
        <DialogHeader>
          <DialogTitle>{r.title(displayName)}</DialogTitle>
          <DialogDescription>{t.skills.publish.scopeHint}</DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 gap-3 overflow-y-auto pr-1 text-xs">
          <ol aria-label={r.action} className="grid gap-1.5">
            <Step
              detail={r.stepCheckDetail}
              note={
                check.isFetching
                  ? r.stepRunning(checkSeconds)
                  : check.isError
                    ? check.error instanceof Error
                      ? check.error.message
                      : r.failed
                    : checked?.cached
                      ? r.stepCached
                      : r.stepDone
              }
              state={check.isFetching ? 'running' : check.isError ? 'failed' : 'done'}
              title={r.stepCheck}
            />
            <Step
              action={
                checked?.conforms && !delisted && !forceAi ? (
                  <Button disabled={busy} onClick={() => setForceAi(true)} size="xs" variant="text">
                    {r.refineAnyway}
                  </Button>
                ) : null
              }
              detail={r.stepRefineDetail}
              note={
                refineState === 'skipped'
                  ? delisted
                    ? r.stepSkipDelisted
                    : r.stepSkipConforms
                  : refineState === 'running'
                    ? r.stepRunning(refineSeconds)
                    : refineState === 'failed'
                      ? refine.error instanceof Error
                        ? refine.error.message
                        : r.failed
                      : refineState === 'done'
                        ? refine.data?.cached
                          ? r.stepCached
                          : r.stepDone
                        : ''
              }
              state={refineState}
              title={r.stepRefine}
            />
          </ol>

          {(check.isError || refine.isError) && !busy && (
            <div className="flex justify-end gap-2" role="alert">
              <Button onClick={onClose} size="sm" variant="text">
                {t.common.cancel}
              </Button>
              <Button onClick={() => void (check.isError ? check.refetch() : refine.refetch())} size="sm">
                {r.retry}
              </Button>
            </div>
          )}

          {checked && !busy && (
            <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
              <span>{r.checkedAt(timeOf(checked.checked_at))}</span>
              {checked.cached && <span>{r.cachedHint}</span>}
              <Button className="ml-auto" onClick={recheck} size="xs" variant="text">
                <RefreshCw className="size-3" />
                {r.recheck}
              </Button>
            </div>
          )}

          {ready && checked && (
            <Review
              aiError={skipAi ? '' : (refine.data?.ai_error ?? '')}
              changelog={refine.data?.changelog ?? ''}
              check={checked}
              draft={refine.data?.draft ?? checked.original}
              findings={refine.data?.findings ?? checked.findings}
              key={`${generation}-${forceAi}-${refine.data?.refined_at ?? 'original'}`}
              onClose={onClose}
              skillName={skillName}
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

type StepState = 'done' | 'failed' | 'running' | 'skipped' | 'waiting'

function Step({
  action,
  detail,
  note,
  state,
  title
}: {
  action?: ReactNode
  detail: string
  note: string
  state: StepState
  title: string
}) {
  return (
    <li
      aria-busy={state === 'running'}
      className="flex flex-wrap items-center gap-2 rounded-md border border-(--ui-stroke-tertiary) px-2.5 py-2"
    >
      <span className="grid size-4 shrink-0 place-items-center">
        {state === 'running' ? (
          <Loader2 className="size-3.5 animate-spin text-(--ui-text-secondary)" />
        ) : state === 'done' ? (
          <Check className="size-3.5 text-emerald-500" />
        ) : (
          <span
            className={cn('size-1.5 rounded-full', state === 'failed' ? 'bg-destructive' : 'bg-(--ui-text-quaternary)')}
          />
        )}
      </span>
      <span className={cn('font-medium', state === 'waiting' && 'text-muted-foreground')}>{title}</span>
      <span className="text-muted-foreground">{detail}</span>
      <span
        className={cn(
          'ml-auto',
          state === 'failed'
            ? 'text-destructive'
            : state === 'running'
              ? 'text-(--ui-text-secondary)'
              : 'text-muted-foreground'
        )}
      >
        {note}
      </span>
      {action}
    </li>
  )
}

interface ReviewProps {
  aiError: string
  changelog: string
  check: SkillPublishCheck
  draft: string
  findings: SkillRefineFinding[]
  onClose: () => void
  skillName: string
}

function Review({ aiError, changelog: initialChangelog, check, draft, findings, onClose, skillName }: ReviewProps) {
  const { t } = useI18n()
  const r = t.skills.refine
  const cache = useQueryClient()
  const [text, setText] = useState(draft)
  const [kept, setKept] = useState<ReadonlySet<string>>(new Set())
  const [changelog, setChangelog] = useState(initialChangelog)
  const [replaceLocal, setReplaceLocal] = useState(true)
  const [slug, setSlug] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<null | SkillPublishResult>(null)

  const status = check.status
  const left = unresolvedFindings(findings, text, kept)
  const slugNeeded = status.status === 'taken'
  const slugValid = !slugNeeded || (SLUG.test(slug) && slug !== check.slug)
  const canPublish = !busy && left.length === 0 && status.status !== 'delisted' && slugValid && text.trim().length > 0

  const toggleKeep = (value: string) =>
    setKept(current => {
      const next = new Set(current)

      if (next.has(value)) {
        next.delete(value)
      } else {
        next.add(value)
      }

      return next
    })

  const publish = async () => {
    setBusy(true)
    setError('')

    try {
      setResult(
        await publishRefinedSkill({
          changelog: changelog.trim(),
          kept: [...kept],
          name: skillName,
          replaceLocal,
          skillMd: text,
          ...(slugNeeded ? { slug } : {})
        })
      )
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['skills-list'] }),
        cache.invalidateQueries({ queryKey: ['skill-hub-mine'] }),
        cache.invalidateQueries({ queryKey: ['skill-hub-catalog'] })
      ])
      invalidateSlashCompletions()
    } catch (err) {
      notifyError(err, r.failed)
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="grid gap-3 text-xs">
        <p className={result.scan_warning || !result.published ? 'text-amber-500' : 'text-emerald-500'}>
          {result.message}
        </p>
        {result.local_backup && <p className="text-muted-foreground">{r.backedUp(result.local_backup)}</p>}
        <DialogFooter>
          <Button onClick={onClose} size="sm">
            {t.common.close}
          </Button>
        </DialogFooter>
      </div>
    )
  }

  const statusLine =
    status.status === 'available'
      ? r.statusAvailable
      : status.status === 'yours'
        ? r.statusYours((status.version ?? 0) + 1)
        : status.status === 'taken'
          ? r.statusTaken(check.slug, status.owner_username ?? '')
          : r.statusDelisted(status.delisted_reason ?? '')

  return (
    <div className="grid gap-3">
      <p
        className={
          status.status === 'taken' || status.status === 'delisted' ? 'text-amber-500' : 'text-muted-foreground'
        }
      >
        {statusLine}
      </p>
      {slugNeeded && (
        <label className="grid max-w-sm gap-1">
          <span className="font-medium">{r.newSlug}</span>
          <Input
            aria-invalid={slug.length > 0 && !slugValid}
            onChange={event => setSlug(event.target.value.trim())}
            value={slug}
          />
          <span className="text-muted-foreground">{r.newSlugHint}</span>
        </label>
      )}
      {aiError && <p className="text-amber-500">{aiError}</p>}
      {check.scan.verdict !== 'safe' && check.scan.findings.length > 0 && (
        <p className="text-amber-500">{r.scanWarning(check.scan.verdict, check.scan.findings.length)}</p>
      )}
      {check.spec_issues.length > 0 && (
        <details className="text-muted-foreground">
          <summary className="cursor-pointer">{r.specTitle(check.spec_issues.length)}</summary>
          <ul className="mt-1 grid list-disc gap-0.5 pl-5">
            {check.spec_issues.map(issue => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </details>
      )}
      {check.skipped.length > 0 && <p className="text-muted-foreground">{r.skipped(check.skipped.length)}</p>}

      <div className="grid gap-3 md:grid-cols-2">
        <div className="grid min-w-0 gap-1">
          <span className="font-medium">{r.original}</span>
          <pre
            className="max-h-80 min-h-60 overflow-auto whitespace-pre-wrap wrap-break-word rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) p-2 font-mono text-[0.68rem]"
            data-selectable-text="true"
          >
            {check.original}
          </pre>
        </div>
        <label className="grid min-w-0 gap-1">
          <span className="font-medium">{r.draft}</span>
          <Textarea
            className="max-h-80 min-h-60 font-mono text-[0.68rem]"
            onChange={event => setText(event.target.value)}
            value={text}
          />
        </label>
      </div>

      <section className="grid gap-1.5">
        <span className="font-medium">{r.findingsTitle(findings.length)}</span>
        <span className="text-muted-foreground">{findings.length ? r.findingsHint : r.noFindings}</span>
        {findings.map(finding => {
          const isKept = kept.has(finding.text)
          const inSkillMd = finding.file === 'SKILL.md'
          const replaced = inSkillMd && !isKept && !text.includes(finding.text)

          return (
            <div
              className="flex flex-wrap items-center gap-2 rounded-md border border-(--ui-stroke-tertiary) px-2 py-1.5"
              key={finding.id}
            >
              <span className="text-muted-foreground">
                {finding.label} · {finding.source === 'ai' ? r.aiSource : r.ruleSource}
              </span>
              <code className="rounded bg-(--ui-bg-tertiary) px-1">{finding.text}</code>
              {!inSkillMd && <span className="text-muted-foreground">{r.otherFile(finding.file)}</span>}
              <span className="ml-auto flex items-center gap-1.5">
                {replaced ? (
                  <span className="text-emerald-500">{r.replaced}</span>
                ) : isKept ? (
                  <>
                    <span className="text-emerald-500">{r.kept}</span>
                    <Button onClick={() => toggleKeep(finding.text)} size="xs" variant="text">
                      {r.undoKeep}
                    </Button>
                  </>
                ) : (
                  <>
                    {inSkillMd && (
                      <Button
                        onClick={() => setText(current => current.split(finding.text).join(finding.placeholder))}
                        size="xs"
                        variant="secondary"
                      >
                        {r.replaceWith(finding.placeholder)}
                      </Button>
                    )}
                    <Button onClick={() => toggleKeep(finding.text)} size="xs" variant="text">
                      {r.keep}
                    </Button>
                  </>
                )}
              </span>
            </div>
          )
        })}
      </section>

      <label className="grid gap-1">
        <span className="font-medium">{r.changelog}</span>
        <Textarea onChange={event => setChangelog(event.target.value)} rows={2} value={changelog} />
      </label>
      <label className="flex items-center gap-2">
        <Checkbox checked={replaceLocal} onCheckedChange={value => setReplaceLocal(value === true)} />
        <span>{r.replaceLocal}</span>
      </label>
      {error && (
        <p className="text-destructive" role="alert">
          {error}
        </p>
      )}
      <DialogFooter>
        {left.length > 0 && <span className="mr-auto text-amber-500">{r.unresolved(left.length)}</span>}
        <Button onClick={onClose} size="sm" variant="text">
          {t.common.cancel}
        </Button>
        <Button disabled={!canPublish} onClick={() => void publish()} size="sm">
          {busy && <Loader2 className="size-3 animate-spin" />}
          {busy ? r.publishing : r.publish}
        </Button>
      </DialogFooter>
    </div>
  )
}
