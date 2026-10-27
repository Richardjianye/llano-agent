import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'
import type * as React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'

import { ArchiveSkillConfirmDialog } from '@/app/learning/archive-skill-confirm-dialog'
import { CodeEditor } from '@/components/chat/code-editor'
import { PageLoader } from '@/components/page-loader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { CountSkeleton } from '@/components/ui/skeleton'
import {
  completeLocalSkillMetadata,
  dismissShareSuggestion,
  editLearningNode,
  getLearningNode,
  getSkills,
  getToolsets,
  getUsageAnalytics,
  setSkillEnabled,
  setToolsetEnabled
} from '@/hermes'
import { useI18n } from '@/i18n'
import { isDesktopToolsetVisible } from '@/lib/desktop-toolsets'
import { compactNumber } from '@/lib/format'
import { queryClient, writeCache } from '@/lib/query-client'
import { invalidateSlashCompletions } from '@/lib/slash-completion-cache'
import { normalize } from '@/lib/text'
import { useStoreSelector } from '@/lib/use-session-slice'
import { $gateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { setCompanySkillUpdates } from '@/store/skill-updates'
import type { SkillInfo, ToolsetInfo } from '@/types/hermes'

import { useOnProfileSwitch } from '../hooks/use-on-profile-switch'
import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import {
  CapRow,
  DetailColumn,
  DetailPane,
  ListColumn,
  ListStrip,
  ListStripButton,
  ListStripMenu,
  type ListStripMenuToggle,
  MasterDetail,
  ToolChip
} from '../master-detail'
import { PanelEmpty, PanelPill } from '../overlays/panel'
import { PageSearchShell } from '../page-search-shell'
import { SETTINGS_ROUTE } from '../routes'
import { ComputerUsePanel } from '../settings/computer-use-panel'
import { asText, includesQuery, toolNames, toolsetDisplayLabel } from '../settings/helpers'
import { TerminalBackendPanel } from '../settings/terminal-backend-panel'
import { ToolsetConfigPanel } from '../settings/toolset-config-panel'
import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'

import chineseCategories from './categories.zh.json'
import { CompanyUpdateAll, companyUpdateLabel, CompanyUpdatePanel, companyUpdatesAvailable } from './company-update'
import { SkillsHub } from './hub'
// CUSTOM（决策 #59）：技能页的「＋ 添加技能」，取代原来藏在「浏览技能中心」里的导入
import { ImportSkillButton } from './import-skill'
import { McpTab } from './mcp-tab'
// CUSTOM: 个人技能空间的发布对话框（阶段 22）
import { publishHintLabel } from './publish-hint'
import { RefinePublishDialog } from './refine-publish-dialog'
import { $skillsSortDesc, $toolsetsSortDesc } from './store'

// CUSTOM（2026-10-16）：同事最常用的是浏览技能中心，排第一并作为默认页签。
const SKILLS_MODES = ['hub', 'skills', 'toolsets', 'mcp'] as const

// Skills + toolsets live in the RQ cache so switching tabs/pages paints the
// cached lists instantly (no reload flash) and mount only fires a deduped
// background refetch. A profile swap globally invalidates (see store/profile),
// so these plain keys refetch against the new backend automatically.
const SKILLS_QUERY_KEY = ['skills-list'] as const
const TOOLSETS_QUERY_KEY = ['toolsets-list'] as const

// Optimistic write-through: toggles/bulk/archive repaint instantly; the next
// background refetch reconciles with the backend.
const setSkills = writeCache<SkillInfo[]>(SKILLS_QUERY_KEY)
const setToolsets = writeCache<ToolsetInfo[]>(TOOLSETS_QUERY_KEY)

// Per-tool call counts come from a 365-day message scan — heavy, and purely
// cosmetic (Toolsets usage badges). Cache the result module-wide with a TTL so
// bouncing between tabs/pages doesn't re-run the scan every time. Keyed by
// profile: analytics are profile-scoped, so a switch must not show the previous
// profile's counts. `useRefreshHotkey` still forces a fresh pull.
const TOOL_CALLS_TTL_MS = 10 * 60 * 1000
const toolCallsCache = new Map<string, { at: number; value: Record<string, number> }>()

async function loadToolCalls(force = false): Promise<Record<string, number>> {
  const key = normalizeProfileKey($activeGatewayProfile.get())
  const cached = toolCallsCache.get(key)

  if (!force && cached && Date.now() - cached.at < TOOL_CALLS_TTL_MS) {
    return cached.value
  }

  const analytics = await getUsageAnalytics(365)

  const value = Object.fromEntries((analytics.tools ?? []).map(e => [e.tool, e.count]))

  // Only cache if the active profile hasn't changed during the request — else a
  // switch mid-flight would file this result under the wrong profile's key.
  if (normalizeProfileKey($activeGatewayProfile.get()) === key) {
    toolCallsCache.set(key, { at: Date.now(), value })
  }

  return value
}

const usageOf = (skill: SkillInfo): number => (typeof skill.usage === 'number' ? skill.usage : 0)

const categoryFor = (skill: SkillInfo): string => asText(skill.category) || 'general'

export const skillCategoryLabel = (category: string): string =>
  (chineseCategories as Record<string, string>)[category.toLowerCase()] ||
  (/[\u3400-\u9fff]/.test(category) ? category : '其他技能')

// CUSTOM（决策 #59）：列表和详情显示 SKILL.md 里的中文展示名；没写的（随包英文技能）回落到 name。
// `name` 仍是 ASCII 标识——目录名、/name 命令、hub slug 都依赖它——所以详情页会把它作为小标签留着。
export const skillLabel = (skill: SkillInfo): string => asText(skill.display_name).trim() || skill.name

// Row subtitle: category, with non-default origins badged.
function skillSubtitle(skill: SkillInfo, updateLabel?: null | string): React.ReactNode {
  const category = skillCategoryLabel(categoryFor(skill))
  const provenance = skill.provenance

  return (
    <>
      <span className="truncate">{category}</span>
      {provenance === 'agent' && (
        <Badge className="shrink-0 normal-case" variant="default">
          习得技能
        </Badge>
      )}
      {provenance === 'hub' && (
        <Badge className="shrink-0 normal-case" variant="muted">
          技能库
        </Badge>
      )}
      {updateLabel && (
        <Badge className="shrink-0 normal-case" variant="warn">
          {updateLabel}
        </Badge>
      )}
    </>
  )
}

const skillRowId = (name: string) => `skill-row-${name}`

export function filteredSkills(skills: SkillInfo[], query: string, desc: boolean): SkillInfo[] {
  const q = normalize(query)
  const sign = desc ? 1 : -1

  return skills
    .filter(
      skill =>
        !q ||
        includesQuery(skill.name, q) ||
        includesQuery(skill.display_name, q) ||
        includesQuery(skill.description, q) ||
        includesQuery(skill.category, q) ||
        includesQuery(skillCategoryLabel(categoryFor(skill)), q)
    )
    .sort((a, b) => sign * (usageOf(b) - usageOf(a)) || asText(a.name).localeCompare(asText(b.name)))
}

const toolsetCalls = (toolset: ToolsetInfo, toolCalls: Record<string, number>): number =>
  toolNames(toolset).reduce((sum, name) => sum + (toolCalls[name] ?? 0), 0)

function filteredToolsets(
  toolsets: ToolsetInfo[],
  query: string,
  toolCalls: Record<string, number>,
  desc: boolean
): ToolsetInfo[] {
  const q = normalize(query)
  const sign = desc ? 1 : -1

  return toolsets
    .filter(toolset => {
      if (!isDesktopToolsetVisible(toolset.name)) {
        return false
      }

      if (!q) {
        return true
      }

      return (
        includesQuery(toolset.name, q) ||
        includesQuery(toolsetDisplayLabel(toolset), q) ||
        includesQuery(toolset.description, q) ||
        toolNames(toolset).some(name => includesQuery(name, q))
      )
    })
    .sort(
      (a, b) =>
        sign * (toolsetCalls(b, toolCalls) - toolsetCalls(a, toolCalls)) ||
        toolsetDisplayLabel(a).localeCompare(toolsetDisplayLabel(b))
    )
}

const visibleToolsetCount = (toolsets: ToolsetInfo[]) => toolsets.filter(ts => isDesktopToolsetVisible(ts.name)).length

interface SkillsViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
}

export function SkillsView({ setStatusbarItemGroup: _setStatusbarItemGroup, ...props }: SkillsViewProps) {
  const { t } = useI18n()
  const [mode, setMode] = useRouteEnumParam('tab', SKILLS_MODES, 'hub')
  // $gateway only feeds the MCP tab — gate the subscription so Skills/Toolsets/Hub
  // tabs don't re-render on connect/disconnect/reconnect.
  const gateway = useStoreSelector($gateway, g => (mode === 'mcp' ? g : null))

  const [query, setQuery] = useState('')

  const {
    data: skills,
    isError: skillsFailed,
    error: skillsError
  } = useQuery({
    queryKey: SKILLS_QUERY_KEY,
    queryFn: getSkills,
    staleTime: 0,
    refetchInterval: query => (query.state.data?.some(skill => skill.metadata_pending) ? 2500 : false)
  })

  const { data: toolsets, isError: toolsetsFailed } = useQuery({
    queryKey: TOOLSETS_QUERY_KEY,
    queryFn: getToolsets,
    staleTime: 0
  })

  // tool name -> call count over the analytics window. null = still loading
  // (badges show skeletons); {} = loaded empty / unavailable backend.
  const [toolCalls, setToolCalls] = useState<Record<string, number> | null>(null)
  // Bumped on profile switch so a slow analytics load from profile A can't set
  // toolCalls after the user moved to B.
  const toolCallsEpoch = useRef(0)
  const skillsSortDesc = useStore($skillsSortDesc)
  const toolsetsSortDesc = useStore($toolsetsSortDesc)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [metadataBusy, setMetadataBusy] = useState(false)
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null)
  const [selectedToolset, setSelectedToolset] = useState<string | null>(null)

  const refreshCapabilities = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: TOOLSETS_QUERY_KEY })
    ])

    invalidateSlashCompletions()

    // An explicit refresh is the one time we bypass the analytics TTL — but
    // only if the badges are already on screen; otherwise let the lazy load
    // pick it up when Toolsets is first shown. Guard the async set against a
    // profile switch landing before it resolves.
    if (toolCallsCache.size > 0) {
      const epoch = toolCallsEpoch.current

      loadToolCalls(true)
        .then(value => toolCallsEpoch.current === epoch && setToolCalls(value))
        .catch(() => toolCallsEpoch.current === epoch && setToolCalls({}))
    }
  }, [])

  // 列表里已经带着更新判定：同步给侧栏小红点，员工点完更新它立即跟着变。
  useEffect(() => {
    if (skills) {
      setCompanySkillUpdates(companyUpdatesAvailable(skills))
    }
  }, [skills])

  const metadataPending = skills?.some(skill => skill.metadata_pending) ?? false

  // 只数能由本机补的：公司技能的中文归服务端、随包技能的归安装包（2026-10-16 起点按钮才调 AI）。
  const metadataMissing = skills?.filter(skill => skill.metadata_incomplete && skill.metadata_completable).length ?? 0

  async function retryMetadata() {
    setMetadataBusy(true)

    try {
      await completeLocalSkillMetadata()
      await queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY })
    } catch (error) {
      notifyError(error, t.skills.metadataFailed)
    } finally {
      setMetadataBusy(false)
    }
  }

  const refreshToolsets = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: TOOLSETS_QUERY_KEY })
  }, [])

  useRefreshHotkey(refreshCapabilities)

  // Per-tool call counts feed ONLY the Toolsets tab's usage badges/sort, and
  // the query behind them is a 365-day message scan — heavy. Fetch it lazily
  // the first time Toolsets is shown, never on Skills or MCP, so it can't
  // starve the MCP tab's config load. Absent → toolsets sort A–Z until it lands.
  useEffect(() => {
    if (mode !== 'toolsets' || toolCalls !== null) {
      return
    }

    let cancelled = false
    // Guard the setter by epoch too: when toolCalls is already null at switch
    // time, setToolCalls(null) is a no-op so this effect never re-runs to flip
    // `cancelled` — the epoch check catches that gap.
    const epoch = toolCallsEpoch.current
    const live = () => !cancelled && toolCallsEpoch.current === epoch

    loadToolCalls()
      .then(value => live() && setToolCalls(value))
      .catch(() => live() && setToolCalls({}))

    return () => void (cancelled = true)
  }, [mode, toolCalls])

  // On a profile switch the analytics cache is profile-keyed, but our local
  // toolCalls state isn't — leaving it non-null would keep the lazy effect from
  // ever re-running, so badges/sort would show the previous profile's counts.
  // Reset to null so the next Toolsets view reloads for the active profile.
  useOnProfileSwitch(() => {
    toolCallsEpoch.current += 1
    setToolCalls(null)
  })

  const visibleSkills = useMemo(
    () => (skills ? filteredSkills(skills, query, skillsSortDesc) : []),
    [query, skills, skillsSortDesc]
  )

  const visibleToolsets = useMemo(
    () => (toolsets ? filteredToolsets(toolsets, query, toolCalls ?? {}, toolsetsSortDesc) : []),
    [query, toolCalls, toolsets, toolsetsSortDesc]
  )

  // Bulk actions ("All" master switch, "Disable unused") and the master-switch
  // state target the WHOLE tab, never the search-filtered view — a tab-wide
  // control that silently scoped to the current query would be a lie.
  const bulkSkills = skills ?? []
  const bulkToolsets = useMemo(() => (toolsets ?? []).filter(ts => isDesktopToolsetVisible(ts.name)), [toolsets])

  // Rotating placeholder nudges from the user's own data — teach that search
  // understands categories and tool names, not just titles.
  const searchHints = useMemo(() => {
    if (mode === 'skills' && skills?.length) {
      const counts = new Map<string, number>()

      for (const skill of skills) {
        const key = categoryFor(skill)
        counts.set(key, (counts.get(key) || 0) + 1)
      }

      return [...counts.entries()]
        .sort(([, a], [, b]) => b - a)
        .slice(0, 5)
        .map(([category]) => t.common.tryHint(skillCategoryLabel(category)))
    }

    if (mode === 'toolsets' && toolsets?.length) {
      return toolsets
        .filter(ts => isDesktopToolsetVisible(ts.name) && toolNames(ts).length > 0)
        .slice(0, 5)
        .map(ts => t.common.tryHint(toolNames(ts)[0]))
    }

    return undefined
  }, [mode, skills, toolsets, t])

  // Keep a valid selection: fall back to the first visible row when the
  // current selection is filtered out (or nothing is selected yet).
  const activeSkill = useMemo(
    () => visibleSkills.find(s => s.name === selectedSkill) ?? visibleSkills[0] ?? null,
    [selectedSkill, visibleSkills]
  )

  const activeToolset = useMemo(
    () => visibleToolsets.find(ts => ts.name === selectedToolset) ?? visibleToolsets[0] ?? null,
    [selectedToolset, visibleToolsets]
  )

  // Single toggles are optimistic and silent on success (the row repaints
  // immediately — a toast per flip would spam rapid customization). Errors
  // revert and notify.
  async function handleToggleSkill(skill: SkillInfo, enabled: boolean) {
    setSkills(current => current?.map(row => (row.name === skill.name ? { ...row, enabled } : row)) ?? current)

    try {
      await setSkillEnabled(skill.name, enabled)
      // A disabled skill loses its `/name` command, so the composer's cached
      // `/` list has to be dropped along with the row repaint.
      invalidateSlashCompletions()
    } catch (err) {
      setSkills(
        current => current?.map(row => (row.name === skill.name ? { ...row, enabled: !enabled } : row)) ?? current
      )
      notifyError(err, t.skills.failedToUpdate(skill.name))
    }
  }

  async function handleToggleToolset(toolset: ToolsetInfo, enabled: boolean) {
    setToolsets(
      current =>
        current?.map(row => (row.name === toolset.name ? { ...row, enabled, available: enabled } : row)) ?? current
    )

    try {
      await setToolsetEnabled(toolset.name, enabled)
    } catch (err) {
      setToolsets(
        current =>
          current?.map(row => (row.name === toolset.name ? { ...row, enabled: !enabled, available: !enabled } : row)) ??
          current
      )
      notifyError(err, t.skills.failedToUpdate(toolsetDisplayLabel(toolset)))
    }
  }

  // Sequential on purpose: each toggle is a config read-modify-write on the
  // backend; parallel calls would race the disabled-list save.
  async function bulkApply(skillTargets: SkillInfo[], toolsetTargets: ToolsetInfo[], enabled: boolean) {
    if (bulkBusy || skillTargets.length + toolsetTargets.length === 0) {
      return
    }

    setBulkBusy(true)

    let done = 0

    try {
      for (const row of skillTargets) {
        await setSkillEnabled(row.name, enabled)
        setSkills(cur => cur?.map(r => (r.name === row.name ? { ...r, enabled } : r)) ?? cur)
        done += 1
      }

      for (const row of toolsetTargets) {
        await setToolsetEnabled(row.name, enabled)
        setToolsets(cur => cur?.map(r => (r.name === row.name ? { ...r, enabled, available: enabled } : r)) ?? cur)
        done += 1
      }

      notify({ kind: 'success', title: t.skills.bulkUpdated(done), message: '' })
    } catch (err) {
      notifyError(err, t.skills.failedToUpdate(mode === 'skills' ? t.skills.tabSkills : t.skills.tabToolsets))
    } finally {
      invalidateSlashCompletions()
      setBulkBusy(false)
    }
  }

  const bulkToggle = (enabled: boolean) =>
    mode === 'skills'
      ? bulkApply(
          bulkSkills.filter(row => row.enabled !== enabled),
          [],
          enabled
        )
      : bulkApply(
          [],
          bulkToolsets.filter(row => row.enabled !== enabled),
          enabled
        )

  // "Never used" = zero recorded activity. The pruning move for a 100+ skill
  // install: keep the workhorses, shed the noise.
  const disableUnused = () =>
    bulkApply(
      bulkSkills.filter(skill => skill.enabled && usageOf(skill) === 0),
      [],
      false
    )

  // One switch line covering enable-all/disable-all.
  const bulkSwitch = (allEnabled: boolean): ListStripMenuToggle => ({
    checked: allEnabled,
    disabled: bulkBusy,
    label: t.skills.all,
    onToggle: checked => void bulkToggle(checked)
  })

  const allSkillsEnabled = bulkSkills.length > 0 && bulkSkills.every(s => s.enabled)
  const allToolsetsEnabled = bulkToolsets.length > 0 && bulkToolsets.every(ts => ts.enabled)

  const sortButton = (desc: boolean, flip: () => void) => (
    <ListStripButton onClick={flip}>{desc ? t.skills.sortMostUsedDesc : t.skills.sortLeastUsedAsc}</ListStripButton>
  )

  // Full-bleed empty state, matching the MCP tab (spans both columns, not a
  // cramped note in the left rail). Query-aware, and says "tools" not the
  // internal "toolsets".
  const capabilityEmpty = (noun: string, action?: React.ReactNode) => {
    const q = query.trim()

    return (
      <div className="flex h-full min-h-0 flex-1">
        <PanelEmpty
          action={q ? undefined : action}
          description={q ? t.skills.emptyNothingMatches(q) : t.skills.emptyNoneAvailable(noun)}
          icon="search"
          title={t.skills.emptyNoneFound(noun)}
        />
      </div>
    )
  }

  // Learned/local skills are editable + archivable, mirroring the memory
  // graph (same /api/learning/node endpoints — delete archives, restorable
  // via `hermes curator restore`).
  const [skillEditor, setSkillEditor] = useState<null | { content: string; name: string }>(null)
  const [skillDraft, setSkillDraft] = useState('')
  const [skillSaving, setSkillSaving] = useState(false)
  const [archiveTarget, setArchiveTarget] = useState<null | string>(null)
  // CUSTOM（阶段 22）：正在发布到部门技能空间的技能名
  const [refineTarget, setRefineTarget] = useState<null | SkillInfo>(null)
  // Bumped on profile switch so an in-flight openSkillEditor fetch from profile
  // A can't reopen the editor with A's content after switching to B.
  const skillEditorEpoch = useRef(0)

  // A profile switch swaps the backend under the open editor/archive dialog —
  // their targets belong to profile A, so a save/archive would hit B. Drop them
  // so nothing edits or archives against the newly active profile.
  useOnProfileSwitch(() => {
    skillEditorEpoch.current += 1
    setSkillEditor(null)
    setSkillDraft('')
    setArchiveTarget(null)
  })

  const openSkillEditor = async (name: string) => {
    const epoch = skillEditorEpoch.current

    try {
      const node = await getLearningNode(name)

      if (skillEditorEpoch.current !== epoch) {
        return
      }

      setSkillEditor({ content: node.content, name })
      setSkillDraft(node.content)
    } catch (err) {
      notifyError(err, name)
    }
  }

  const saveSkillEdit = async () => {
    if (!skillEditor) {
      return
    }

    setSkillSaving(true)

    try {
      await editLearningNode(skillEditor.name, skillDraft)
      notify({
        kind: 'success',
        title: t.skills.skillUpdated,
        message: t.skills.appliesToNewSessions(skillEditor.name)
      })
      setSkillEditor(null)
      void refreshCapabilities()
    } catch (err) {
      notifyError(err, skillEditor.name)
    } finally {
      setSkillSaving(false)
    }
  }

  const [revealSkill, setRevealSkill] = useState<string | null>(null)

  // CUSTOM（决策 #59）：添加成功后清掉搜索词并选中新技能，同事一眼就能看到它落在哪
  const handleSkillImported = useCallback((name: string) => {
    setQuery('')
    setSelectedSkill(name)
    setRevealSkill(name)
  }, [])

  // 新技能用过 0 次，按「最常用」排在近百个技能的最底下；不滚过去，同事会以为没添加上。
  useEffect(() => {
    if (!revealSkill || !visibleSkills.some(skill => skill.name === revealSkill)) {
      return
    }

    document.getElementById(skillRowId(revealSkill))?.scrollIntoView({ block: 'center' })
    setRevealSkill(null)
  }, [revealSkill, visibleSkills])

  const skillEditorPane = skillEditor && (
    <DetailPane
      actions={
        <Button disabled={skillSaving} onClick={() => void saveSkillEdit()} size="xs">
          {skillSaving ? t.common.saving : t.common.save}
        </Button>
      }
      id="skill-editor"
      onClose={() => setSkillEditor(null)}
      title={<span className="text-[0.68rem] font-normal text-muted-foreground/60">{skillEditor.name}/SKILL.md</span>}
    >
      <CodeEditor
        filePath="SKILL.md"
        initialValue={skillEditor.content}
        key={skillEditor.name}
        onCancel={() => setSkillEditor(null)}
        onChange={setSkillDraft}
        onSave={() => void saveSkillEdit()}
      />
    </DetailPane>
  )

  return (
    <PageSearchShell
      {...props}
      activeTab={mode}
      onSearchChange={setQuery}
      onTabChange={id => setMode(id as (typeof SKILLS_MODES)[number])}
      // MCP manages a handful of entries with the editor right there —
      // searching it is noise.
      searchHidden={mode === 'mcp'}
      searchHints={searchHints}
      searchPlaceholder={
        mode === 'skills'
          ? t.skills.searchSkills
          : mode === 'hub'
            ? t.skills.hub.searchPlaceholder
            : t.skills.searchToolsets
      }
      searchValue={query}
      tabs={[
        { id: 'hub', label: t.skills.tabHub },
        { id: 'skills', label: t.skills.tabSkills, meta: skills?.length ?? null },
        { id: 'toolsets', label: t.skills.tabToolsets, meta: toolsets ? visibleToolsetCount(toolsets) : null },
        { id: 'mcp', label: t.skills.tabMcp }
      ]}
    >
      {mode === 'hub' ? (
        <SkillsHub
          companyUpdates={companyUpdatesAvailable(skills)}
          onAddLocalSkill={() => setMode('skills')}
          query={query}
        />
      ) : mode === 'mcp' ? (
        <McpTab gateway={gateway} />
      ) : (skillsFailed || toolsetsFailed) && (!skills || !toolsets) ? (
        <PanelEmpty
          action={
            <Button onClick={() => void refreshCapabilities()} size="sm">
              {t.skills.refresh}
            </Button>
          }
          description={skillsError instanceof Error ? skillsError.message : undefined}
          icon="error"
          title={t.skills.skillsLoadFailed}
        />
      ) : !skills || !toolsets ? (
        <PageLoader label={t.skills.loading} />
      ) : mode === 'skills' ? (
        visibleSkills.length === 0 ? (
          capabilityEmpty('skills', <ImportSkillButton onImported={handleSkillImported} />)
        ) : (
          <MasterDetail pane={skillEditorPane} split="wide">
            <ListColumn
              header={
                <>
                  {/* CUSTOM（决策 #59）：添加入口 + 一行说明。说明必须挨着按钮——分不清「添加到我这里」
                      和「上传到技能中心」，正是原来那个入口被误解的原因。 */}
                  <div className="mb-1.5 grid gap-1 px-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <ImportSkillButton onImported={handleSkillImported} />
                      {!metadataPending && metadataMissing > 0 && (
                        <Button
                          disabled={metadataBusy}
                          onClick={() => void retryMetadata()}
                          size="xs"
                          title={t.skills.metadataHint}
                          variant="secondary"
                        >
                          {t.skills.metadataRetry(metadataMissing)}
                        </Button>
                      )}
                    </div>
                    <p className="text-[0.65rem] leading-4 text-(--ui-text-tertiary)">{t.skills.import.caption}</p>
                    <CompanyUpdateAll count={companyUpdatesAvailable(skills)} />
                    {metadataPending && (
                      <p className="text-xs text-(--ui-text-tertiary)" role="status">
                        {t.skills.metadataGenerating}
                      </p>
                    )}
                  </div>
                  <ListStrip
                    left={sortButton(skillsSortDesc, () => $skillsSortDesc.set(!$skillsSortDesc.get()))}
                    right={
                      <ListStripMenu
                        items={[
                          { disabled: bulkBusy, label: t.skills.disableUnused, onSelect: () => void disableUnused() }
                        ]}
                        label={t.skills.tabSkills}
                        toggle={bulkSwitch(allSkillsEnabled)}
                      />
                    }
                  />
                </>
              }
            >
              {visibleSkills.map(skill => (
                <CapRow
                  active={activeSkill?.name === skill.name}
                  busy={bulkBusy}
                  enabled={skill.enabled}
                  key={skill.name}
                  meta={
                    <span className="flex flex-col items-end gap-0.5">
                      <span>{t.skills.hub.downloads(skill.download_count)}</span>
                      <span>{t.skills.usageCount(usageOf(skill))}</span>
                    </span>
                  }
                  onSelect={() => setSelectedSkill(skill.name)}
                  onToggle={enabled => void handleToggleSkill(skill, enabled)}
                  rowId={skillRowId(skill.name)}
                  subtitle={skillSubtitle(
                    skill,
                    companyUpdateLabel(skill, t.skills.update) ?? publishHintLabel(skill, t.skills.refine)
                  )}
                  title={skillLabel(skill)}
                  toggleLabel={skillLabel(skill)}
                />
              ))}
            </ListColumn>
            <DetailColumn footer={t.skills.changesApplyNewSessions}>
              {activeSkill && (
                <SkillDetail
                  onArchive={() => setArchiveTarget(activeSkill.name)}
                  onEdit={() => void openSkillEditor(activeSkill.name)}
                  onRefine={() => setRefineTarget(activeSkill)}
                  skill={activeSkill}
                />
              )}
            </DetailColumn>
          </MasterDetail>
        )
      ) : visibleToolsets.length === 0 ? (
        capabilityEmpty('tools')
      ) : (
        <MasterDetail split="wide">
          <ListColumn
            header={
              <ListStrip
                left={sortButton(toolsetsSortDesc, () => $toolsetsSortDesc.set(!$toolsetsSortDesc.get()))}
                right={<ListStripMenu label={t.skills.tabToolsets} toggle={bulkSwitch(allToolsetsEnabled)} />}
              />
            }
          >
            {visibleToolsets.map(toolset => {
              const label = toolsetDisplayLabel(toolset)
              const calls = toolCalls ? toolsetCalls(toolset, toolCalls) : null

              return (
                <CapRow
                  active={activeToolset?.name === toolset.name}
                  busy={bulkBusy}
                  enabled={toolset.enabled}
                  key={toolset.name}
                  meta={
                    calls === null ? (
                      <CountSkeleton />
                    ) : calls > 0 ? (
                      `×${compactNumber(calls)}`
                    ) : (
                      `${toolNames(toolset).length} tools`
                    )
                  }
                  onSelect={() => setSelectedToolset(toolset.name)}
                  onToggle={checked => void handleToggleToolset(toolset, checked)}
                  subtitle={asText(toolset.description)}
                  title={label}
                  toggleLabel={t.skills.toggleToolset(label, !toolset.enabled)}
                />
              )
            })}
          </ListColumn>
          <DetailColumn footer={t.skills.changesApplyNewSessions}>
            {activeToolset && (
              <ToolsetDetail onConfiguredChange={refreshToolsets} toolCalls={toolCalls ?? {}} toolset={activeToolset} />
            )}
          </DetailColumn>
        </MasterDetail>
      )}
      {archiveTarget && (
        <ArchiveSkillConfirmDialog
          onApply={() => {
            const name = archiveTarget
            const snapshot = skills

            setSkills(current => current?.filter(skill => skill.name !== name) ?? current)
            invalidateSlashCompletions()

            if (skillEditor?.name === name) {
              setSkillEditor(null)
            }

            return () => setSkills(snapshot)
          }}
          onClose={() => setArchiveTarget(null)}
          onFailure={(err, name) => notifyError(err, name)}
          open
          skillId={archiveTarget}
          skillName={archiveTarget}
        />
      )}
      {/* CUSTOM（2026-10-16，P2）：整理并发布 —— 脱敏 + AI 整理 + 审阅后发布 */}
      {refineTarget && (
        <RefinePublishDialog
          displayName={skillLabel(refineTarget)}
          onClose={() => setRefineTarget(null)}
          skillName={refineTarget.name}
        />
      )}
    </PageSearchShell>
  )
}

// Shared inspector header — mirrors Messaging's PlatformDetail so Skills and
// Tools share one title/description block and tab switches don't jump.
function DetailHeader({
  description,
  pills,
  title
}: {
  description: React.ReactNode
  pills?: React.ReactNode
  title: string
}) {
  return (
    <header>
      <div className="flex min-h-6 flex-wrap items-center gap-2">
        <h3 className="min-w-0 truncate text-[0.9375rem] font-semibold tracking-tight">{title}</h3>
        {pills}
      </div>
      <p className="mt-1 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {description}
      </p>
    </header>
  )
}

/** CUSTOM（2026-10-16，P2）：「值得分享」提示，可按技能「不再提示」。乐观隐藏，失败回滚。 */
function ShareSuggestion({ skill }: { skill: SkillInfo }) {
  const { t } = useI18n()
  const r = t.skills.refine

  const dismiss = async () => {
    const hide = (suggest: boolean) =>
      setSkills(
        current =>
          current?.map(row =>
            row.name === skill.name && row.publish_state
              ? { ...row, publish_state: { ...row.publish_state, suggest } }
              : row
          ) ?? current
      )

    hide(false)

    try {
      await dismissShareSuggestion(skill.name)
    } catch (err) {
      hide(true)
      notifyError(err, r.failed)
    }
  }

  return (
    <p className="flex flex-wrap items-center gap-2 text-xs text-(--ui-text-secondary)">
      <span>{r.suggestHint(usageOf(skill))}</span>
      <Button onClick={() => void dismiss()} size="xs" variant="text">
        {r.dismissSuggest}
      </Button>
    </p>
  )
}

function SkillDetail({
  onArchive,
  onEdit,
  onRefine,
  skill
}: {
  onArchive: () => void
  onEdit: () => void
  onRefine: () => void
  skill: SkillInfo
}) {
  const { t } = useI18n()
  // Only learned/local skills are the user's to rewrite or archive — bundled
  // and hub skills are managed by their sources.
  const editable = skill.provenance === 'agent'
  // CUSTOM（阶段 22）：发布的门槛比编辑宽一档 —— 手写的技能（provenance 为空）
  // 也是这个员工的工作成果，它恰恰是最该进部门技能空间的那一类。
  //
  // 但 `hub` 一定要排除：它是从某个 hub 装下来的，**包括我们自己的 llano-hub**。
  // 把它再发一次，要么撞上网关的「只有首发人能发新版」，要么把别人的技能挂到
  // 自己名下 —— 两个结果都不是员工按这个按钮时想要的。
  // `bundled` 是上游随包发的，发到公司库里只是噪音。
  const publishable = skill.provenance !== 'bundled' && skill.provenance !== 'hub'

  return (
    <>
      <DetailHeader
        description={asText(skill.description) || t.skills.noDescription}
        pills={
          <>
            <PanelPill>{skillCategoryLabel(categoryFor(skill))}</PanelPill>
            {skill.display_name && skill.display_name !== skill.name && (
              <PanelPill tone="muted">{skill.name}</PanelPill>
            )}
            {skill.provenance && skill.provenance !== 'bundled' && (
              <PanelPill tone={skill.provenance === 'agent' ? 'good' : 'muted'}>
                {t.skills.provenance[skill.provenance]}
              </PanelPill>
            )}
          </>
        }
        title={skillLabel(skill)}
      />
      <CompanyUpdatePanel skill={skill} />
      {publishable && skill.publish_state?.suggest && <ShareSuggestion skill={skill} />}
      <p className="text-xs text-(--ui-text-tertiary)">
        {t.skills.hub.downloads(skill.download_count)} · {t.skills.usageCount(usageOf(skill))}
        {skill.usage_count != null && <> · {t.skills.hub.calls(skill.usage_count)}</>}
      </p>
      {(editable || publishable) && (
        <div className="flex items-center gap-2">
          {editable && (
            <Button onClick={onEdit} size="xs" variant="text">
              {t.skills.edit}
            </Button>
          )}
          {/* 2026-10-16 起只保留「整理并发布」：合规的技能机器检查一过就能直接发 */}
          {publishable && (
            <Button onClick={onRefine} size="xs" variant="textStrong">
              {t.skills.refine.action}
            </Button>
          )}
          {editable && (
            <Button className="text-destructive hover:text-destructive" onClick={onArchive} size="xs" variant="text">
              {t.skills.archive}
            </Button>
          )}
        </div>
      )}
    </>
  )
}

function ToolsetDetail({
  toolset,
  toolCalls,
  onConfiguredChange
}: {
  toolset: ToolsetInfo
  toolCalls: Record<string, number>
  onConfiguredChange: () => void
}) {
  const { t } = useI18n()
  const navigate = useNavigate()
  const tools = toolNames(toolset)
  const label = toolsetDisplayLabel(toolset)

  return (
    <>
      {/* "Configured" as a resting state is noise — only the warn state earns a pill. */}
      <DetailHeader
        description={asText(toolset.description) || t.skills.noDescription}
        pills={!toolset.configured && <PanelPill tone="warn">{t.skills.needsKeys}</PanelPill>}
        title={label}
      />
      {tools.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {tools.map(name => (
            <ToolChip key={name}>
              {name}
              {(toolCalls[name] ?? 0) > 0 && (
                <span className="ml-1 text-(--ui-text-quaternary)">×{compactNumber(toolCalls[name])}</span>
              )}
            </ToolChip>
          ))}
        </div>
      )}
      {toolset.name === 'vision' && (
        // Vision has no provider matrix — model resolution runs through the
        // auxiliary model config. Point at the actual home (Settings → Models,
        // aux "vision" row) via an internal deep link instead of leaving the
        // detail pane empty.
        <div className="grid gap-1.5">
          <p className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
            {t.skills.visionModelHint}
          </p>
          <div>
            <Button
              onClick={() => navigate(`${SETTINGS_ROUTE}?tab=config:model&aux=vision`)}
              size="xs"
              variant="textStrong"
            >
              {t.skills.visionModelLink}
            </Button>
          </div>
        </div>
      )}
      {toolset.name === 'computer_use' && <ComputerUsePanel onConfiguredChange={onConfiguredChange} />}
      {toolset.name === 'terminal' && <TerminalBackendPanel onConfiguredChange={onConfiguredChange} />}
      <ToolsetConfigPanel key={toolset.name} onConfiguredChange={onConfiguredChange} toolset={toolset.name} />
    </>
  )
}
