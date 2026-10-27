import { describe, expect, it } from 'vitest'
import source from '@/pages/prompt-policy.vue?raw'

describe('prompt policy page write controls', () => {
  it('gates draft editing separately from publishing and rollback', () => {
    expect(source).toContain(':readonly="editorLocked"')
    expect(source).toMatch(/v-if="canEdit"[\s\S]*?text="保存草稿"/)
    expect(source).toMatch(/v-if="canPublish"[\s\S]*?text="发布草稿"/)
    expect(source).toMatch(/v-if="canPublish && item\.version !== published\?\.version"/)
  })

  it('warns that published changes apply to new sessions only', () => {
    expect(source).toContain('发布只影响之后创建的新会话')
    expect(source).toContain('已存在的会话不受影响')
  })
})
