import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { keepCompanySkillVersion, type SkillInfo, updateAllCompanySkills, updateCompanySkill } from '@/hermes'
import { useI18n } from '@/i18n'
import { Loader2 } from '@/lib/icons'
import { invalidateSlashCompletions } from '@/lib/slash-completion-cache'
import { notify, notifyError } from '@/store/notifications'

const SKILLS_LIST_KEY = ['skills-list'] as const

/** 列表上的状态小字：有新版本 / 已下架；没有就不显示。 */
export function companyUpdateLabel(
  skill: SkillInfo,
  u: { badge: (version: number | string) => string; delisted: string }
): null | string {
  const state = skill.hub_update

  if (state?.delisted) {
    return u.delisted
  }

  return state?.update_available ? u.badge(state.latest_version ?? '') : null
}

export const companyUpdatesAvailable = (skills: SkillInfo[] | undefined) =>
  skills?.filter(skill => skill.hub_update?.update_available).length ?? 0

function useRefreshSkills() {
  const cache = useQueryClient()

  return async () => {
    await Promise.all([
      cache.invalidateQueries({ queryKey: SKILLS_LIST_KEY }),
      cache.invalidateQueries({ queryKey: ['skill-hub-sources'] })
    ])
    invalidateSlashCompletions()
  }
}

/**
 * CUSTOM（2026-10-16，P1）：技能详情里的公司技能更新。
 *
 * 更新由员工手动确认（负责人定）。本机改过的技能不给「直接更新」：只能「备份我的修改后更新」
 * 或「保留我的版本」（这一版不再提示），绝不静默覆盖 —— 后台复盘常会往技能里补步骤，
 * 那些是员工自己的积累。
 */
export function CompanyUpdatePanel({ skill }: { skill: SkillInfo }) {
  const { t } = useI18n()
  const u = t.skills.update
  const refresh = useRefreshSkills()
  const [busy, setBusy] = useState(false)
  const state = skill.hub_update

  if (!state || (!state.update_available && !state.delisted)) {
    return null
  }

  if (state.delisted) {
    return <p className="text-xs text-amber-500">{u.delistedHint(state.delisted_reason ?? '')}</p>
  }

  const run = async (action: () => Promise<void>) => {
    setBusy(true)

    try {
      await action()
      await refresh()
    } catch (error) {
      notifyError(error, u.failed)
    } finally {
      setBusy(false)
    }
  }

  const update = (backup: boolean) =>
    run(async () => {
      const result = await updateCompanySkill(skill.name, backup)

      notify({
        kind: 'success',
        title: u.updated(skill.display_name || skill.name, result.version ?? state.latest_version ?? ''),
        message: result.backup ? `${u.backedUp(result.backup)} ${u.newSessions}` : u.newSessions
      })
    })

  const keep = () =>
    run(async () => {
      await keepCompanySkillVersion(skill.name, state.latest_bundle_hash ?? '')
      notify({ kind: 'success', title: u.kept, message: u.newSessions })
    })

  return (
    <div className="grid gap-2 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) p-3 text-xs">
      <p className="font-medium text-foreground/85">
        {u.available(state.installed_version ?? '?', state.latest_version ?? '?')}
      </p>
      {(state.publisher_username || state.published_at) && (
        <p className="text-(--ui-text-tertiary)">
          {t.skills.hub.publishedBy(
            state.publisher_username ?? '',
            (state.published_at ?? '').replace('T', ' ').slice(0, 16)
          )}
        </p>
      )}
      {state.changelog && <p className="text-(--ui-text-secondary)">{u.changelog(state.changelog)}</p>}
      {state.locally_modified && <p className="text-amber-500">{u.localChanges}</p>}
      <div className="flex flex-wrap items-center gap-2">
        {state.locally_modified ? (
          <>
            <Button disabled={busy} onClick={() => void update(true)} size="xs" variant="secondary">
              {busy && <Loader2 className="size-3 animate-spin" />}
              {busy ? u.updating : u.backupAndUpdate}
            </Button>
            <Button disabled={busy || !state.latest_bundle_hash} onClick={() => void keep()} size="xs" variant="text">
              {u.keepMine}
            </Button>
          </>
        ) : (
          <Button disabled={busy} onClick={() => void update(false)} size="xs" variant="secondary">
            {busy && <Loader2 className="size-3 animate-spin" />}
            {busy ? u.updating : u.update}
          </Button>
        )}
      </div>
    </div>
  )
}

/** 列表顶部的汇总与「全部更新」：只更新本机没改过的，改过的列出来逐个确认。 */
export function CompanyUpdateAll({ count }: { count: number }) {
  const { t } = useI18n()
  const u = t.skills.update
  const refresh = useRefreshSkills()
  const [busy, setBusy] = useState(false)

  if (count === 0) {
    return null
  }

  const updateAll = async () => {
    setBusy(true)

    try {
      const result = await updateAllCompanySkills()
      const skipped = result.locally_modified.length

      notify({
        kind: skipped > 0 || result.failed.length > 0 ? 'warning' : 'success',
        title: u.updateAllDone(result.updated.filter(item => item.updated).length, skipped),
        message: [
          skipped > 0 ? u.updateAllSkipped(result.locally_modified.join('、')) : '',
          ...result.failed.map(item => `${item.name}：${item.error}`),
          u.newSessions
        ]
          .filter(Boolean)
          .join('\n')
      })
      await refresh()
    } catch (error) {
      notifyError(error, u.failed)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center justify-between gap-2 rounded-md bg-sky-500/10 px-2 py-1.5 text-[0.7rem] text-sky-600 dark:text-sky-300">
      <span>{u.summary(count)}</span>
      <Button disabled={busy} onClick={() => void updateAll()} size="xs" variant="secondary">
        {busy && <Loader2 className="size-3 animate-spin" />}
        {busy ? u.updating : u.updateAll}
      </Button>
    </div>
  )
}
