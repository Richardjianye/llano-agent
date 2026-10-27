import type { Department } from '@/types/api'
import { describe, expect, it } from 'vitest'
import { filterDepartments } from '@/pages/department-filter'

const DEPARTMENTS: Department[] = [
  {
    id: 1,
    code: 'visual-center',
    name: '视觉中心',
    parent_id: null,
    status: 1,
    remark: '负责品牌视觉',
  },
  {
    id: 2,
    code: 'SUPPLY-CHAIN-MERCH',
    name: '商品供应链',
    parent_id: null,
    status: 1,
    remark: '采购与履约',
  },
  {
    id: 3,
    code: 'general-office',
    name: '综合办公室',
    parent_id: null,
    status: 1,
    remark: null,
  },
]

describe('filterDepartments', () => {
  it.each([
    ['视觉', [1]],
    ['supply-chain', [2]],
    ['履约', [2]],
  ])('按名称、编码或备注包含匹配：%s', (keyword, expectedIds) => {
    expect(filterDepartments(DEPARTMENTS, keyword).map(item => item.id)).toEqual(expectedIds)
  })

  it('忽略搜索词首尾空格和英文字母大小写', () => {
    expect(filterDepartments(DEPARTMENTS, '  supply-CHAIN  ').map(item => item.id)).toEqual([2])
  })

  it('空搜索词保留全部部门及原有顺序', () => {
    expect(filterDepartments(DEPARTMENTS, ' '.repeat(3))).toEqual(DEPARTMENTS)
  })

  it('搜索框清空为 null 时保留全部部门', () => {
    expect(filterDepartments(DEPARTMENTS, null)).toEqual(DEPARTMENTS)
  })

  it('备注为空时仍可安全过滤', () => {
    expect(filterDepartments(DEPARTMENTS, '不存在')).toEqual([])
  })
})
