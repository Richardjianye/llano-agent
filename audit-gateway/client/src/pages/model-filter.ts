import type { ModelAdminInfo } from '@/types/api'

export const DEFAULT_HIDE_DISABLED_MODELS = true

export function filterModelsByStatus (
  models: ModelAdminInfo[],
  hideDisabled = DEFAULT_HIDE_DISABLED_MODELS,
): ModelAdminInfo[] {
  return hideDisabled ? models.filter(model => model.status === 1) : models
}
