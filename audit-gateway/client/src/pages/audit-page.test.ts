import { describe, expect, it } from 'vitest'
import source from '@/pages/audit.vue?raw'

describe('audit page session turns pagination', () => {
  it('offers a load-more button instead of stopping at the first 100 turns', () => {
    expect(source).toMatch(/v-else-if="hasMoreTurns\(item\.session_key\)"[\s\S]*?加载更多/)
    expect(source).toContain('@click="loadMoreTurns(item.session_key)"')
    expect(source).not.toContain('只列出了前')
  })

  it('appends the next page, dedupes by id and fixes the page-boundary repeat flag', () => {
    expect(source).toContain('page: nextPage')
    expect(source).toContain('new Set(current.map(t => t.id))')
    expect(source).toContain('repeats_question: true')
  })

  it('resets per-session pagination state when the session list reloads', () => {
    expect(source).toMatch(/turns\.value = \{\}\s+turnsPage\.value = \{\}\s+turnsTotal\.value = \{\}/)
  })
})
