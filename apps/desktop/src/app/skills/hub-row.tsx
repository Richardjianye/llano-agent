import { useStore } from '@nanostores/react'

import { Button } from '@/components/ui/button'
import { type SkillHubResult } from '@/hermes'
import { useI18n } from '@/i18n'
import { Loader2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $hubActions, $hubInstalledOverride, installHubSkill, uninstallHubSkill } from '@/store/hub-actions'
import { notify, notifyError } from '@/store/notifications'

export function trustTone(level: string): string {
  switch (level) {
    case 'builtin':
      return 'bg-(--ui-bg-tertiary) text-(--ui-text-secondary)'

    case 'trusted':
      return 'bg-emerald-500/15 text-emerald-400'

    default:
      return 'bg-amber-500/15 text-amber-400'
  }
}

/**
 * CUSTOM（2026-09-24）：公司技能库的一行下面写「demo-admin · 2026-09-23 15:39 发布」。
 * 时间是网关的服务器本地时间（北京时间），只截到分钟、去掉「T」，不做时区换算。
 */
export function hubByline(
  skill: Pick<SkillHubResult, 'owner' | 'published_at'>,
  publishedBy: (owner: string, when: string) => string
): string {
  const owner = (skill.owner ?? '').trim()
  const when = (skill.published_at ?? '').trim().replace('T', ' ').slice(0, 16)

  if (!owner && !when) {
    return ''
  }

  return publishedBy(owner, when)
}

// One hub result — a self-contained tile that installs/uninstalls ITSELF and
// reads its own action status from the store, so parallel installs never desync.
// `rawInstalled` is the sources/search truth; the store's optimistic override
// wins so the row flips the instant its own action resolves.
export function HubSkillRow({ installedName, onPreview, rawInstalled, skill }: HubSkillRowProps) {
  const { t } = useI18n()
  const h = t.skills.hub
  const action = useStore($hubActions)[skill.identifier]
  const override = useStore($hubInstalledOverride)[skill.identifier]
  const installed = override ?? rawInstalled
  const running = action?.running ?? false

  const byline = hubByline(skill, h.publishedBy)

  const doInstall = () => {
    notify({ kind: 'success', title: h.installStarted(skill.name), message: h.actionLog })
    void installHubSkill(skill.identifier).catch(err => notifyError(err, h.actionFailed))
  }

  const doUninstall = () => {
    notify({ kind: 'success', title: h.uninstallStarted(skill.name), message: h.actionLog })
    void uninstallHubSkill(skill.identifier, installedName || skill.name).catch(err => notifyError(err, h.actionFailed))
  }

  return (
    <article className="row-hover flex min-w-0 flex-col gap-3 rounded-lg border border-(--ui-stroke-tertiary) p-4">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <h3 className="line-clamp-2 break-words text-sm font-medium text-foreground/85">{skill.name}</h3>
          <span className={cn('rounded px-1.5 py-0.5 text-[0.6rem]', trustTone(skill.trust_level))}>
            {h.trust[skill.trust_level] ?? skill.trust_level}
          </span>
          {skill.mine && (
            <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[0.6rem] text-sky-500">{h.mineBadge}</span>
          )}
          {installed && <span className="text-[0.6rem] text-emerald-400">{h.installed}</span>}
        </div>
        <p className="mt-2 line-clamp-3 break-words text-xs text-muted-foreground">{skill.description}</p>
        {skill.group && <p className="mt-2 truncate text-[0.65rem] text-(--ui-text-tertiary)">{skill.group}</p>}
        {byline && <p className="mt-1 break-words text-[0.62rem] text-(--ui-text-tertiary)">{byline}</p>}
        <p className="mt-2 text-[0.65rem] text-(--ui-text-tertiary)">
          {h.downloads(skill.download_count)} · {h.calls(skill.usage_count)}
        </p>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button onClick={() => onPreview(skill)} size="xs" variant="text">
          {h.preview}
        </Button>
        {installed ? (
          <Button className="hover:text-destructive" disabled={running} onClick={doUninstall} size="xs" variant="text">
            {running && <Loader2 className="size-3 animate-spin" />}
            {running ? h.uninstalling : h.uninstall}
          </Button>
        ) : (
          <Button disabled={running} onClick={doInstall} size="xs" variant="textStrong">
            {running && <Loader2 className="size-3 animate-spin" />}
            {running ? h.installing : h.install}
          </Button>
        )}
      </div>
    </article>
  )
}

interface HubSkillRowProps {
  installedName: null | string
  onPreview: (skill: SkillHubResult) => void
  rawInstalled: boolean
  skill: SkillHubResult
}
