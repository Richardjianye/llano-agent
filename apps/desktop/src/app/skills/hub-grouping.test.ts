import { describe, expect, it } from 'vitest'

import type { SkillHubResult } from '@/types/hermes'

import { groupByDepartment, shouldRenderGroups } from './hub'

/**
 * Landing-page grouping for the Browse-hub tab (2026-09-03).
 *
 * The tab used to show the public index's featured strip, which is empty on a
 * branded build — so the landing was a blank page and nobody searched. It now
 * leads with the company's own library, grouped by department.
 */

function skill(identifier: string, group?: string): SkillHubResult {
  return {
    description: '',
    group,
    identifier,
    name: identifier,
    repo: null,
    source: 'llano-hub',
    tags: [],
    trust_level: 'trusted'
  }
}

describe('groupByDepartment', () => {
  it('keeps the order the gateway sent, not alphabetical order', () => {
    // The gateway sorts company picks first, then the viewer's own department.
    // Sorting again here would bury the viewer's own department in the middle —
    // the exact problem the grouping exists to prevent.
    const groups = groupByDepartment([skill('a', '公司精选'), skill('b', '视觉设计部'), skill('c', '财务中心')])

    expect(groups.map(([name]) => name)).toEqual(['公司精选', '视觉设计部', '财务中心'])
  })

  it('collects every skill of one department into a single section', () => {
    // Non-adjacent rows of the same department must still land in one bucket:
    // two "视觉设计部" headings on one page reads as a rendering bug.
    const groups = groupByDepartment([skill('a', '视觉设计部'), skill('b', '财务中心'), skill('c', '视觉设计部')])

    expect(groups).toHaveLength(2)
    expect(groups[0][1].map(s => s.identifier)).toEqual(['a', 'c'])
  })

  it('treats a blank group as ungrouped rather than as a department named " "', () => {
    const groups = groupByDepartment([skill('a', '   '), skill('b')])

    expect(groups).toHaveLength(1)
    expect(groups[0][0]).toBe('')
  })
})

describe('shouldRenderGroups', () => {
  it('draws headings when the company hub grouped the rows', () => {
    expect(shouldRenderGroups(groupByDepartment([skill('a', '财务中心'), skill('b', '视觉设计部')]))).toBe(true)
  })

  it('draws a heading even for a single named department', () => {
    // One department is still information: it says whose skill this is.
    expect(shouldRenderGroups(groupByDepartment([skill('a', '财务中心')]))).toBe(true)
  })

  it('skips headings when nothing is grouped', () => {
    // The public index sends no group. One blank heading over every row is
    // worse than no headings at all.
    expect(shouldRenderGroups(groupByDepartment([skill('a'), skill('b')]))).toBe(false)
  })

  it('skips headings for an empty list', () => {
    expect(shouldRenderGroups([])).toBe(false)
  })
})
