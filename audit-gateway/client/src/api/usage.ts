/** 用量统计接口 —— 对应 server/router/admin/usage.py */

import type {
  UsageBreakdownResult,
  UsageFilterOptions,
  UsageRecordPage,
  UsageSummary,
} from '@/types/api'
import { http } from '@/api/http'

/** 查询条件 —— 四个接口共用，后端会在数据范围内进一步收窄 */
export interface UsageQuery {
  start?: string
  end?: string
  granularity?: string
  trend_interval?: 'auto' | '5m'
  dept_id?: number
  user_uid?: string
  role?: string
  model_id?: number
}

/** 顶部指标卡 + 趋势图 */
export function fetchUsageSummary (query: UsageQuery = {}) {
  return http.get<UsageSummary>('/admin/usage/summary', { ...query })
}

/** 按维度下钻的表格 */
export function fetchUsageBreakdown (dimension: string, query: UsageQuery = {}, limit = 100) {
  return http.get<UsageBreakdownResult>('/admin/usage/breakdown', {
    dimension,
    limit,
    ...query,
  })
}

/** 单次调用明细（唯一会查明细表的接口，强制分页） */
export function fetchUsageRecords (
  query: UsageQuery & { status?: string, page?: number, page_size?: number } = {},
) {
  return http.get<UsageRecordPage>('/admin/usage/records', { ...query })
}

/** 筛选下拉框候选项（只含范围内确实有用量的部门与模型） */
export function fetchUsageFilters () {
  return http.get<UsageFilterOptions>('/admin/usage/filters')
}

/**
 * 导出 CSV
 *
 * 这个接口返回的是文件流而不是 {code,message,data} 信封，
 * 所以不能走 http 封装，得自己发请求再触发下载。
 */
export function buildExportUrl (dimension: string, query: UsageQuery = {}): string {
  const params = new URLSearchParams({ dimension })
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') {
      params.append(key, String(value))
    }
  }
  return `/admin/usage/export?${params.toString()}`
}
