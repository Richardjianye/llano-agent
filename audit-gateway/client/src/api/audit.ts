/**
 * 会话审计接口 —— 对应 server/router/admin/audit.py
 *
 * ⚠️ **每次调用这里的函数，后端都会记一条查阅日志**（决策 #8：
 * 管理部门查看行为本身也要留痕）。所以：
 *
 * - 别为了「预加载」而提前批量调 `fetchAuditDetail`，那会凭空造出
 *   一堆「查看全文」记录，把真正的查阅行为淹没；
 * - 详情抽屉必须是用户点击才拉取，不能跟着列表一起加载。
 *
 * 列表接口刻意不下发全文，正是为了让 detail 那条日志能精确到
 * 「谁看了谁的哪一条」。前端不要试图缓存全文来省请求。
 */

import type {
  AuditAccessLogPage,
  AuditDetail,
  AuditFilterOptions,
  AuditListPage,
  AuditSessionPage,
} from '@/types/api'
import { http } from '@/api/http'

/** 列表查询条件 */
export interface AuditQuery {
  start?: string
  end?: string
  dept_id?: number
  user_uid?: string
  role?: string
  model_id?: number
  status?: string
  has_attachment?: boolean
  keyword?: string
  /** 精确定位某次调用 —— 从用量明细复制过来的请求ID */
  request_id?: string
  /** 只看某个会话内的轮次（展开会话行时用） */
  session_id?: string
  /** 连标题生成、命令审批扫描一起显示。默认关：它们会把噪声顶到最前面 */
  include_machine_turns?: boolean
}

/** 会话列表（只有摘要，没有全文） */
export function fetchAuditList (
  query: AuditQuery & { page?: number, page_size?: number } = {},
) {
  return http.get<AuditListPage>('/admin/audit/list', { ...query })
}

/**
 * **会话列表** —— 一行一个会话，不是一行一次调用（决策 #52 · C1）
 *
 * 展开某一行看里面每一轮，走的还是 `fetchAuditList`，带上 `session_id`。
 * 不另开接口是刻意的：否则会变成两套摘要逻辑，迟早只改其中一套。
 */
export function fetchAuditSessions (
  query: AuditQuery & { page?: number, page_size?: number } = {},
) {
  return http.get<AuditSessionPage>('/admin/audit/sessions', { ...query })
}

/** 会话详情 —— 唯一会拿到全文的接口，**必须由用户点击触发** */
export function fetchAuditDetail (id: number) {
  return http.get<AuditDetail>(`/admin/audit/detail/${id}`)
}

/** 筛选下拉框候选项（列全部部门与模型，不只是「有记录的」） */
export function fetchAuditFilters () {
  return http.get<AuditFilterOptions>('/admin/audit/filters')
}

/** 查阅日志的筛选条件 */
export interface AccessLogQuery {
  start?: string
  end?: string
  actor_uid?: string
  action?: string
  /**
   * 按**被查阅人**筛选 —— 这张表最有价值的用法
   *
   * 按查阅人翻只能看出「这个审计员很勤快」；
   * 按被查阅人翻才回答得了「有没有人在专门盯着某个人的会话」。
   */
  target_user_uid?: string
}

/** 查阅日志 —— 看这个页面本身不会再产生新日志 */
export function fetchAccessLogs (
  query: AccessLogQuery & { page?: number, page_size?: number } = {},
) {
  return http.get<AuditAccessLogPage>('/admin/audit/access-logs', { ...query })
}

function withBase (path: string, params: URLSearchParams): string {
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

/**
 * 附件原件地址
 *
 * 返回文件流而不是 {code,message,data} 信封，所以不走 http 封装。
 * 图片直接丢给 <img src>，非图片走新窗口下载。
 * 两种用法都会在后端留下一条 attachment 查阅日志。
 */
export function buildAttachmentUrl (attachmentId: number): string {
  return withBase(`/admin/audit/attachment/${attachmentId}`, new URLSearchParams())
}

/**
 * 导出 CSV —— 只含摘要不含全文
 *
 * 全文导出等于把会话库打包带走，一旦落到本地磁盘就脱离了留存期限
 * 与访问控制的管辖（TODO 第 5 节合规动作），所以后端刻意没做。
 */
export function buildAuditExportUrl (query: AuditQuery = {}): string {
  return withBase('/admin/audit/export', toParams(query))
}

/**
 * 导出查阅日志 CSV —— 给合规检查/内部核查交材料用
 *
 * 与会话导出的取舍不同，这份可以放心导：**一个字的聊天内容都没有**，
 * 只有「谁在什么时候看了谁的什么」。会话导出之所以只出摘要，
 * 是因为全文落到本地磁盘就脱离了留存期限管辖 —— 这份没有那个问题。
 */
export function buildAccessLogExportUrl (query: AccessLogQuery = {}): string {
  return withBase('/admin/audit/access-logs/export', toParams(query))
}

function toParams (query: object): URLSearchParams {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') {
      params.append(key, String(value))
    }
  }
  return params
}
