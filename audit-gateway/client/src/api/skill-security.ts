/**
 * 技能安全事件接口 —— 对应 server/router/admin/skill_report.py
 *
 * 写入侧是客户端上报（server/router/v1/skill_reports.py），管理端**只读**：
 * 本期没有「标记已审」之类的写接口，复核结论怎么回到客户端是下一期的事。
 *
 * ## 三个接口分三层，是权限决定的，不是为了好看
 *
 * - `fetchSkillEvents` 列表：一律不下发样本内容，哪怕调用方两个权限点都有。
 *   一页四十条全带上，等于把几十个人的代码一次性推给任何打得开这个页面的人。
 * - `fetchSkillEvent` 详情：命中项明细 + 同包的其它事件，仍然不含代码。
 * - `fetchSkillSample` 样本：员工写的源码，后端另判 `skill_security:sample`。
 *
 * 所以**不要**为了让详情抽屉"更流畅"而跟着详情一起把样本预取了 —— 那会让
 * 只有 :view 权限的人每开一个详情就撞一次 403，也让将来加查阅日志时
 * 「谁取过代码」这条记录失去意义。
 */

import type {
  SkillEventDetail,
  SkillEventPage,
  SkillSample,
} from '@/types/api'
import { http } from '@/api/http'

/** 列表查询条件。留空的项不传，由后端决定默认范围 */
export interface SkillEventQuery {
  /** skill_quarantined / skill_quarantine_restored */
  event?: string
  /** dangerous / caution */
  verdict?: string
  dept_id?: number
  user_uid?: string
  /** 后端按 LIKE 匹配，可以只填一段 */
  skill_name?: string
}

/** 事件列表 —— 只有摘要，没有 findings，更没有代码 */
export function fetchSkillEvents (
  query: SkillEventQuery & { page?: number, page_size?: number } = {},
) {
  return http.get<SkillEventPage>('/admin/skill-events/list', { ...query })
}

/** 事件详情：命中项明细 + 同一 bundle_hash 的其它事件。仍然不含代码 */
export function fetchSkillEvent (id: number) {
  return http.get<SkillEventDetail>(`/admin/skill-events/detail/${id}`)
}

/**
 * 样本本体 —— 唯一会拿到员工源码的接口
 *
 * 必须由用户点击触发。后端要求 `skill_security:view` **且**
 * `skill_security:sample`，只有前者的人调这里会拿到 403。
 */
export function fetchSkillSample (sampleId: number) {
  return http.get<SkillSample>(`/admin/skill-events/sample/${sampleId}`)
}
