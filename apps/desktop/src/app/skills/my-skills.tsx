import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { getMyPublishedSkills, type MyPublishedSkill, type SkillHubResult } from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $activeGatewayProfile } from '@/store/profile'

import { hubByline } from './hub-row'
import { RefinePublishDialog } from './refine-publish-dialog'

export const MY_SKILLS_KEY = ['skill-hub-mine'] as const

/** 预览走技能中心的同一个对话框，只需要把「我发布的」一行拼成公司技能库的结果形状。 */
export function asHubResult(skill: MyPublishedSkill): SkillHubResult {
  return {
    description: skill.description,
    download_count: skill.download_count ?? null,
    identifier: `llano-hub/${skill.slug}`,
    mine: true,
    name: skill.name,
    published_at: skill.published_at,
    repo: null,
    source: 'llano-hub',
    tags: [],
    trust_level: 'trusted',
    usage_count: skill.usage_count ?? null
  }
}

/**
 * CUSTOM（2026-10-16，负责人定）：作者管理自己发布的技能的单独页面。
 *
 * 列出网关上本人首发的全部技能（含已下架和下架原因），以及本机是否还有对应技能、
 * 发布后是否又改过 —— 后台复盘会持续改进本机技能，这些改进不发新版本就到不了同事那里。
 * 不在这里做下架：下架是主管/管理员的权限，作者有问题直接沟通。
 */
export function MySkills({ onPreview }: { onPreview: (skill: SkillHubResult) => void }) {
  const { t } = useI18n()
  const h = t.skills.hub
  const profile = useStore($activeGatewayProfile)
  const [publishing, setPublishing] = useState<MyPublishedSkill | null>(null)

  const query = useQuery({
    queryKey: [...MY_SKILLS_KEY, profile],
    queryFn: getMyPublishedSkills,
    staleTime: 30_000
  })

  if (query.isPending) {
    return <PageLoader className="min-h-40" label={h.searching} />
  }

  if (query.isError) {
    return (
      <div className="py-6 text-xs text-muted-foreground" role="alert">
        {query.error instanceof Error ? query.error.message : h.catalogFailed}{' '}
        <Button onClick={() => void query.refetch()} size="xs" variant="text">
          {h.retry}
        </Button>
      </div>
    )
  }

  const skills = query.data.skills

  if (skills.length === 0) {
    return <p className="py-10 text-center text-xs text-(--ui-text-tertiary)">{h.mineEmpty}</p>
  }

  return (
    <>
      <div className="grid grid-cols-1 gap-3 @min-[36rem]:grid-cols-2 @min-[56rem]:grid-cols-3">
        {skills.map(skill => {
          const listed = skill.status === 'listed'
          const byline = hubByline({ owner: `v${skill.version}`, published_at: skill.published_at }, h.publishedBy)

          return (
            <article
              className="flex min-w-0 flex-col gap-3 rounded-lg border border-(--ui-stroke-tertiary) p-4"
              key={skill.slug}
            >
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <h3 className="line-clamp-2 break-words text-sm font-medium text-foreground/85">{skill.name}</h3>
                  <span
                    className={cn(
                      'rounded px-1.5 py-0.5 text-[0.6rem]',
                      listed ? 'bg-emerald-500/15 text-emerald-400' : 'bg-amber-500/15 text-amber-400'
                    )}
                  >
                    {listed ? h.mineListed : h.mineDelisted}
                  </span>
                  <span className="rounded bg-(--ui-bg-tertiary) px-1.5 py-0.5 text-[0.6rem] text-(--ui-text-secondary)">
                    {skill.scope === 'company' ? h.mineCompanyPick : skill.scope_dept_name || h.mineDeptSpace}
                  </span>
                </div>
                <p className="mt-2 line-clamp-3 break-words text-xs text-muted-foreground">{skill.description}</p>
                {byline && <p className="mt-2 text-[0.62rem] text-(--ui-text-tertiary)">{byline}</p>}
                {skill.changelog && (
                  <p className="mt-1 line-clamp-2 break-words text-[0.62rem] text-(--ui-text-tertiary)">
                    {h.mineChangelog(skill.changelog)}
                  </p>
                )}
                <p className="mt-2 text-[0.65rem] text-(--ui-text-tertiary)">
                  {h.downloads(skill.download_count)} · {h.calls(skill.usage_count)} ·{' '}
                  {h.mineUsers(skill.user_count ?? 0)}
                </p>
                {!listed && skill.delisted_reason && (
                  <p className="mt-2 text-[0.65rem] text-amber-400">{h.mineDelistedReason(skill.delisted_reason)}</p>
                )}
                {skill.local_changed && <p className="mt-2 text-[0.65rem] text-amber-400">{h.mineLocalChanged}</p>}
              </div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                {listed && (
                  <Button onClick={() => onPreview(asHubResult(skill))} size="xs" variant="text">
                    {h.preview}
                  </Button>
                )}
                {listed && skill.local_name ? (
                  <Button
                    onClick={() => setPublishing(skill)}
                    size="xs"
                    variant={skill.local_changed ? 'textStrong' : 'text'}
                  >
                    {h.minePublishNew}
                  </Button>
                ) : listed ? (
                  <span className="text-[0.6rem] text-(--ui-text-tertiary)">{h.mineNoLocal}</span>
                ) : null}
              </div>
            </article>
          )
        })}
      </div>
      {publishing?.local_name && (
        <RefinePublishDialog
          displayName={publishing.name}
          onClose={() => {
            setPublishing(null)
            void query.refetch()
          }}
          skillName={publishing.local_name}
        />
      )}
    </>
  )
}
