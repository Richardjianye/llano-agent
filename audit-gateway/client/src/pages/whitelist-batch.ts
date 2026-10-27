/** 白名单批量授权与视图的纯逻辑（2026-10-19） */

import type {
  AccessSource,
  ModelGrantOverview,
  WhitelistBatchResult,
  WhitelistTarget,
} from '@/api/catalog'
import { SCOPE } from '@/types/api'

export interface BatchSelection {
  /** 勾了「全员」就只发一条 global，其余对象没意义 */
  everyone: boolean
  deptIds: (number | string)[]
  roles: string[]
  uids: string[]
}

export function emptySelection (): BatchSelection {
  return { everyone: false, deptIds: [], roles: [], uids: [] }
}

/** 把多选结果展开成接口要的对象列表，去重、丢空值 */
export function buildBatchTargets (selection: BatchSelection): WhitelistTarget[] {
  if (selection.everyone) {
    return [{ scope: SCOPE.GLOBAL, scope_value: '' }]
  }
  const targets: WhitelistTarget[] = []
  const push = (scope: string, values: (number | string)[]) => {
    for (const value of new Set(values.map(v => String(v).trim()).filter(Boolean))) {
      targets.push({ scope, scope_value: value })
    }
  }
  push(SCOPE.DEPT, selection.deptIds)
  push(SCOPE.ROLE, selection.roles)
  push(SCOPE.USER, selection.uids)
  return targets
}

/** 本次会生成多少条（模型 × 对象） */
export function batchSize (modelIds: number[], targets: WhitelistTarget[]): number {
  return new Set(modelIds).size * targets.length
}

/** 保存后的提示语。被跳过的显式拒绝要单独说，否则管理员以为已经放开了 */
export function batchSummary (result: WhitelistBatchResult): string {
  const parts = [`新增 ${result.created} 条`]
  const skipped = result.skipped.length
  if (skipped > 0) {
    const denied = result.skipped.filter(s => !s.existing_allowed).length
    parts.push(denied > 0
      ? `跳过已存在 ${skipped} 条（其中 ${denied} 条是显式拒绝，未改动）`
      : `跳过已存在 ${skipped} 条`)
  }
  return parts.join('，')
}

/** 反查来源的展示文字：「部门：视觉中心（上级部门继承）」 */
export function sourceText (source: AccessSource, scopeLabels: Record<string, string>, roleLabel: (role: string) => string): string {
  const scope = scopeLabels[source.scope] ?? source.scope
  if (source.scope === SCOPE.GLOBAL) {
    return scope
  }
  const label = source.scope === SCOPE.ROLE ? roleLabel(source.scope_value) : source.label
  const inherited = source.inherited_from_dept_id === null ? '' : '（上级部门继承）'
  return `${scope}：${label}${inherited}`
}

/** 按模型名/授权对象名过滤概览 */
export function filterOverview (items: ModelGrantOverview[], keyword: string): ModelGrantOverview[] {
  const q = keyword.trim().toLowerCase()
  if (!q) {
    return items
  }
  return items.filter(item => {
    const names = [item.display_name, item.public_name]
    for (const grants of Object.values(item.grants)) {
      names.push(...grants.map(g => g.label))
    }
    return names.some(name => name?.toLowerCase().includes(q))
  })
}

interface DeptLike { id: number, name: string, parent_id: number | null }

/** 「视觉中心 / 设计创意部」—— 选部门时同名的组（如两个「运营组」）才分得清。遇环即停 */
export function departmentPath (deptId: number, departments: DeptLike[]): string {
  const byId = new Map(departments.map(d => [d.id, d]))
  const names: string[] = []
  const seen = new Set<number>()
  let current = byId.get(deptId)
  while (current && !seen.has(current.id)) {
    seen.add(current.id)
    names.unshift(current.name)
    current = current.parent_id === null ? undefined : byId.get(current.parent_id)
  }
  return names.length > 0 ? names.join(' / ') : `#${deptId}`
}
