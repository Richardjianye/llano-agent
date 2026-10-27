import type { ModelAdminInfo } from '@/types/api'
import { describe, expect, it } from 'vitest'
import {
  DEFAULT_HIDE_DISABLED_MODELS,
  filterModelsByStatus,
} from '@/pages/model-filter'

const MODELS = [
  { id: 1, display_name: '启用模型', status: 1 },
  { id: 2, display_name: '停用模型', status: 0 },
] as ModelAdminInfo[]

describe('filterModelsByStatus', () => {
  it('默认隐藏已停用模型', () => {
    expect(DEFAULT_HIDE_DISABLED_MODELS).toBe(true)
    expect(filterModelsByStatus(MODELS).map(model => model.id)).toEqual([1])
  })

  it('关闭隐藏开关后保留全部模型及原有顺序', () => {
    expect(filterModelsByStatus(MODELS, false)).toEqual(MODELS)
  })
})
