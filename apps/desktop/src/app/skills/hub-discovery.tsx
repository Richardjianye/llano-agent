import { useStore } from '@nanostores/react'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { Tabs as TabsPrimitive } from 'radix-ui'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { getSkillHubCatalog, type HubDepartment, type SkillHubInstalledEntry, type SkillHubResult } from '@/hermes'
import { useI18n } from '@/i18n'
import { $activeGatewayProfile } from '@/store/profile'

import { usePublicCatalog } from './hub-public-pages'
import { HubSkillRow } from './hub-row'

interface RowsProps {
  installed: Record<string, SkillHubInstalledEntry>
  onPreview: (skill: SkillHubResult) => void
}

function Rows({ skills, installed, onPreview }: RowsProps & { skills: SkillHubResult[] }) {
  return (
    <div className="grid grid-cols-1 gap-3 @min-[36rem]:grid-cols-2 @min-[56rem]:grid-cols-3">
      {skills.map(skill => (
        <HubSkillRow
          installedName={installed[skill.identifier]?.name ?? null}
          key={skill.identifier}
          onPreview={onPreview}
          rawInstalled={Boolean(installed[skill.identifier])}
          skill={skill}
        />
      ))}
    </div>
  )
}

function Department({ department, ...rows }: RowsProps & { department: HubDepartment }) {
  const { t } = useI18n()
  const h = t.skills.hub
  const profile = useStore($activeGatewayProfile)

  const more = useInfiniteQuery({
    queryKey: ['skill-hub-catalog', profile, 'department', department.id],
    queryFn: ({ pageParam }) => getSkillHubCatalog('company', pageParam, department.id),
    initialPageParam: 0,
    getNextPageParam: page => (page.departments[0]?.has_more ? page.departments[0].next_offset : undefined),
    staleTime: 60_000,
    enabled: department.count > 0
  })

  const skills = [
    ...new Map(
      (more.data?.pages.flatMap(page => page.departments[0]?.skills ?? []) ?? []).map(skill => [
        skill.identifier,
        skill
      ])
    ).values()
  ]

  return (
    <section className="border-b border-(--ui-stroke-tertiary) py-2">
      {more.isPending && department.count > 0 ? (
        <p className="py-4 text-xs text-muted-foreground" role="status">
          {h.searching}
        </p>
      ) : department.count === 0 ? (
        <p className="py-2 text-xs text-muted-foreground">{h.emptyDepartment}</p>
      ) : (
        <Rows {...rows} skills={skills} />
      )}
      {more.isError && (
        <p className="text-xs text-destructive" role="alert">
          {h.catalogFailed}
        </p>
      )}
      {(more.isError || more.hasNextPage) && (
        <Button
          disabled={more.isFetching}
          onClick={() => void (more.isError ? more.refetch() : more.fetchNextPage())}
          size="xs"
          variant="text"
        >
          {more.isError ? h.retry : h.loadMore}
        </Button>
      )}
    </section>
  )
}

export function HubDiscovery(rows: RowsProps) {
  const { t } = useI18n()
  const h = t.skills.hub
  const profile = useStore($activeGatewayProfile)
  const [selection, setSelection] = useState<{ profile: string | null; id: string } | null>(null)

  const company = useQuery({
    queryKey: ['skill-hub-catalog', profile, 'company'],
    queryFn: () => getSkillHubCatalog('company'),
    staleTime: 60_000
  })

  const departments = company.data?.departments ?? []

  const selected =
    selection?.profile === profile && departments.some(d => String(d.id) === selection.id)
      ? selection.id
      : String(departments[0]?.id ?? '')

  const recommended = usePublicCatalog('recommended', 12, profile)
  const packages = usePublicCatalog('packages', 6, profile)

  const status = (query: { isPending: boolean; isError: boolean; refetch: () => unknown }) =>
    query.isPending ? (
      <p className="py-4 text-xs text-muted-foreground" role="status">
        {h.searching}
      </p>
    ) : query.isError ? (
      <div className="py-3 text-xs text-muted-foreground" role="alert">
        {h.catalogFailed}{' '}
        <Button onClick={() => void query.refetch()} size="xs" variant="text">
          {h.retry}
        </Button>
      </div>
    ) : null

  return (
    <div className="space-y-7">
      <section>
        <h2 className="mb-2 text-sm font-medium">{h.companyLibrary}</h2>
        {status(company)}
        {departments.length > 0 && (
          <Tabs onValueChange={id => setSelection({ profile, id })} value={selected}>
            <TabsList aria-label={h.companyLibrary} className="h-auto flex-wrap justify-start gap-1 bg-transparent p-0">
              {departments.map(department => (
                <TabsTrigger
                  className="h-7 rounded border border-(--ui-stroke-tertiary) px-2 text-xs data-[state=active]:border-(--ui-text-tertiary) data-[state=active]:bg-(--ui-bg-tertiary) data-[state=active]:shadow-none"
                  key={department.id}
                  value={String(department.id)}
                >
                  {department.name} <span className="text-muted-foreground">({department.count})</span>
                  {department.is_current && (
                    <span className="text-[0.6rem] text-muted-foreground">{h.currentDepartment}</span>
                  )}
                </TabsTrigger>
              ))}
            </TabsList>
            {departments.map(department => (
              <TabsPrimitive.Content key={`${profile}:${department.id}`} value={String(department.id)}>
                <Department {...rows} department={department} />
              </TabsPrimitive.Content>
            ))}
          </Tabs>
        )}
      </section>
      <section>
        <h2 className="mb-2 text-sm font-medium">{h.recommended}</h2>
        {status(recommended)}
        <Rows {...rows} skills={recommended.data?.pages.flatMap(page => page.skills) ?? []} />
        {recommended.hasNextPage && (
          <Button
            disabled={recommended.isFetching}
            onClick={() => void recommended.fetchNextPage()}
            size="xs"
            variant="text"
          >
            {h.loadMore}
          </Button>
        )}
      </section>
      <section>
        <h2 className="mb-2 text-sm font-medium">{h.packages}</h2>
        {status(packages)}
        <div className="grid gap-3 sm:grid-cols-2">
          {packages.data?.pages
            .flatMap(page => page.packages)
            .map(pack => (
              <article className="rounded-md border border-(--ui-stroke-tertiary) p-3" key={pack.slug}>
                <h3 className="text-xs font-medium">
                  {pack.name} <span className="text-muted-foreground">({pack.skill_count})</span>
                </h3>
                <p className="my-2 line-clamp-3 text-xs text-muted-foreground">{pack.description}</p>
                <a className="text-xs underline" href={pack.detail_url} rel="noreferrer" target="_blank">
                  {h.officialDetails}
                </a>
              </article>
            ))}
        </div>
        {packages.hasNextPage && (
          <Button disabled={packages.isFetching} onClick={() => void packages.fetchNextPage()} size="xs" variant="text">
            {h.loadMore}
          </Button>
        )}
      </section>
    </div>
  )
}
