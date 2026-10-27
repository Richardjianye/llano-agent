/**
 * 屏蔽词 —— 对应 server/router/admin/redaction.py
 *
 * ## ⚠️ 改这里是安全配置，不是内容配置
 *
 * 删一个词等于当场把那条防线关掉，而且**已经发出去的消息不会追溯遮蔽** ——
 * 群聊消息的脱敏发生在写入那一刻，库里存的就是当时的结果。这是对的（历史不该
 * 被改写），但配的人必须知道，所以界面上要说出来。
 *
 * ## 生效词表是三个来源的并集
 *
 * 环境变量 `MASK_KEYWORDS` 基线 ∪ global 行 ∪ 本部门行。所以列表页看不出最终
 * 结果 —— `fetchEffective` 那条专门回答「这个部门实际按哪几个词、什么顺序匹配」。
 * 顺序有意义：长词优先，否则「示例公司科技」会被切成「[公司]科技」。
 */

import type { RedactionRule } from '@/types/api'
import { http } from '@/api/http'

export function fetchRedactionRules (scope = '') {
  return http.get<{ items: RedactionRule[] }>('/admin/redaction/list', scope ? { scope } : {})
}

/** 某部门**实际生效**的词表，按匹配顺序返回。不传部门则只看全局 */
export function fetchEffective (deptId?: number) {
  return http.get<{ keywords: string[] }>('/admin/redaction/effective',
    deptId === undefined ? {} : { dept_id: deptId })
}

export function saveRedactionRule (payload: {
  keyword: string
  scope: string
  scope_value?: string
  remark?: string
  rule_id?: number
  status?: number
}) {
  return http.post<{ id: number }>('/admin/redaction/save', payload)
}

/** 停用（不物理删）—— 留着才能回答「这个词什么时候不再遮了」 */
export function disableRedactionRule (ruleId: number) {
  return http.post<{ id: number }>('/admin/redaction/disable', { rule_id: ruleId })
}
