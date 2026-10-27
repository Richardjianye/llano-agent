import type { ModelGrantOverview } from '@/api/catalog'
import { describe, expect, it } from 'vitest'
import { batchSize, batchSummary, buildBatchTargets, emptySelection, filterOverview, sourceText } from './whitelist-batch'

const LABELS = { global: '全员', dept: '部门', role: '角色', user: '指定用户' }
const role = (r: string) => (r === 'manager' ? '部门负责人' : r)

describe('batch whitelist targets', () => {
  it('expands departments, roles and members into one target list without duplicates', () => {
    const targets = buildBatchTargets({ everyone: false, deptIds: [9, '9', 5], roles: ['manager'], uids: ['u1', ''] })
    expect(targets).toEqual([
      { scope: 'dept', scope_value: '9' },
      { scope: 'dept', scope_value: '5' },
      { scope: 'role', scope_value: 'manager' },
      { scope: 'user', scope_value: 'u1' },
    ])
    expect(batchSize([16, 17, 16], targets)).toBe(8)
  })

  it('collapses to a single global target when everyone is selected', () => {
    expect(buildBatchTargets({ ...emptySelection(), everyone: true, deptIds: [9] }))
      .toEqual([{ scope: 'global', scope_value: '' }])
    expect(buildBatchTargets(emptySelection())).toEqual([])
  })

  it('calls out skipped explicit denies so nobody thinks they were opened', () => {
    expect(batchSummary({ created: 3, skipped: [] })).toBe('新增 3 条')
    expect(batchSummary({
      created: 1,
      skipped: [
        { scope: 'dept', scope_value: '9', model_id: 1, existing_allowed: false },
        { scope: 'role', scope_value: 'manager', model_id: 1, existing_allowed: true },
      ],
    })).toBe('新增 1 条，跳过已存在 2 条（其中 1 条是显式拒绝，未改动）')
  })
})

describe('access views', () => {
  it('marks grants inherited from a parent department', () => {
    expect(sourceText({ scope: 'dept', scope_value: '9', label: '视觉中心', allowed: true, inherited_from_dept_id: 9 }, LABELS, role))
      .toBe('部门：视觉中心（上级部门继承）')
    expect(sourceText({ scope: 'role', scope_value: 'manager', label: 'manager', allowed: true, inherited_from_dept_id: null }, LABELS, role))
      .toBe('角色：部门负责人')
    expect(sourceText({ scope: 'global', scope_value: '', label: '全员', allowed: true, inherited_from_dept_id: null }, LABELS, role))
      .toBe('全员')
  })

  it('filters the per-model overview by model or grantee name', () => {
    const item = (id: number, name: string, dept: string): ModelGrantOverview => ({
      model_id: id, display_name: name, public_name: name, status: 1, rule_count: 1, effective_user_count: 1,
      grants: { global: [], dept: [{ scope_value: '9', label: dept, allowed: true }], role: [], user: [] },
    })
    const items = [item(1, 'GPT-6.1-Sol', '视觉中心'), item(2, 'Seedream', '国内营销中心')]
    expect(filterOverview(items, 'gpt').map(i => i.model_id)).toEqual([1])
    expect(filterOverview(items, '营销').map(i => i.model_id)).toEqual([2])
    expect(filterOverview(items, ' ')).toHaveLength(2)
  })
})

describe('department path', () => {
  it('builds the path from the top and survives a cycle', async () => {
    const { departmentPath } = await import('./whitelist-batch')
    const depts = [
      { id: 9, name: '视觉中心', parent_id: null },
      { id: 30, name: '设计创意部', parent_id: 9 },
      { id: 1, name: 'A', parent_id: 2 },
      { id: 2, name: 'B', parent_id: 1 },
    ]
    expect(departmentPath(30, depts)).toBe('视觉中心 / 设计创意部')
    expect(departmentPath(1, depts)).toBe('B / A')
    expect(departmentPath(404, depts)).toBe('#404')
  })
})
