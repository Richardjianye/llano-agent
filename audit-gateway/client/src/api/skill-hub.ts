/**
 * 自有 skill hub 的管理接口 —— 对应 server/router/admin/skill_hub.py
 *
 * 写入侧是员工发布（server/router/v1/skill_hub.py），这里做两件事：
 * **下架/恢复**，和**技能指标统计**。
 *
 * ## 为什么后台没有「审批」
 *
 * 流程是先发后审：发布即本部门可见（主管晋级后全公司可见），管理员事后可下架。跟技能安全那一套同一个
 * 取向——不拦人、留痕、事后处理。审核队列会卡住技能指标，而指标正是 hub 存在
 * 的理由。
 *
 * ## 列表与版本历史都**不下发文件内容**
 *
 * 与技能安全那边不下发样本是同一条理由：一页几十条全带上，等于把全公司的技能
 * 源码一次性推给任何打得开这个页面的人。
 *
 * 取内容是**单独一条路径**（fetchSkillHubFiles），也是单独一个动作 ——
 * 将来要记查阅日志，挂点就在那里，而不是「打开页面就顺手预取」。
 */

import type { SkillHubFiles, SkillHubPage, SkillHubStats, SkillHubVersion } from '@/types/api'
import { http } from '@/api/http'

export interface SkillHubQuery {
  /** listed / delisted，留空为全部 */
  status?: string
  dept_id?: number
  /** 后端按 LIKE 匹配 slug / 名称 / 发布者，可以只填一段 */
  keyword?: string
}

/** 技能列表。**含已下架** —— 员工来问「我搜不到 xxx 了」时后台得答得上来 */
export function fetchSkillHubList (
  query: SkillHubQuery & { page?: number, page_size?: number } = {},
) {
  return http.get<SkillHubPage>('/admin/skill-hub/list', { ...query })
}

/** 某个技能的版本历史。不含文件内容 */
export function fetchSkillHubVersions (slug: string) {
  return http.get<{ versions: SkillHubVersion[] }>('/admin/skill-hub/versions', { slug })
}

/**
 * 某一版的**文件本体**，用来审阅（2026-09-03）。不传 version 取当前该装的那版。
 *
 * ## 为什么后台需要这条
 *
 * 在这之前，后台看得到技能的名字、发布者、版本数，唯独看不到**它到底是什么**。
 * 要决定下不下架只能凭技能名猜，或者自己去客户端装一份 —— 而下架恰恰是这个
 * 页面唯一的止血手段。技能安全那一页早就能读样本源码，技能库这边缺着是漏做。
 *
 * 权限只要 `skill_hub:view`：这是**已发布**的包，公司里任何人都能在客户端里装
 * 下来读。给它加一道后台才有的门禁挡不住任何人，只会让下架决策继续靠猜。
 */
export function fetchSkillHubFiles (slug: string, version?: number) {
  return http.get<SkillHubFiles>(
    '/admin/skill-hub/files',
    version === undefined ? { slug } : { slug, version },
  )
}

/**
 * 下架 —— 技能对客户端立刻彻底不存在（搜不到、装不了、发不了新版）
 *
 * **不删数据**：已经装到员工机器上的那些不会因为下架而消失。
 * reason 会显示在后台列表里，别留空——下架之后最常见的下一句话就是
 * 「我搜不到 xxx 了」。
 */
export function delistSkill (skillId: number, reason: string) {
  return http.post<{ id: number, slug: string, status: string }>(
    '/admin/skill-hub/delist',
    { skill_id: skillId, reason },
  )
}

/** 恢复上架。会把下架痕迹一起清掉 */
export function relistSkill (skillId: number) {
  return http.post<{ id: number, slug: string, status: string }>(
    '/admin/skill-hub/relist',
    { skill_id: skillId },
  )
}

/** 技能指标：按部门、按人各一份。两个数都是累计，不是本月 */
export function fetchSkillHubStats (limit = 50) {
  return http.get<SkillHubStats>('/admin/skill-hub/stats', { limit })
}

/**
 * 把一个技能从部门空间发到**公司空间** —— 阶段 21
 *
 * **没有管理部门审核那一步**（负责人 2026-08-31 定）：管理部门基本不上线看，而且技能
 * 已经过主管审查了。出问题由主管撤回、或管理部门登录后台下架。多一道没人处理的
 * 审批队列，等于把技能永远卡在部门里。
 *
 * 主管只能晋级自己部门的 —— 判定在服务端，前端藏按钮只是省得让人白点。
 */
export function promoteSkill (skillId: number) {
  return http.post<{ id: number, slug: string, scope: string }>(
    '/admin/skill-hub/promote', { skill_id: skillId },
  )
}

/**
 * 从公司空间撤回部门空间。
 *
 * ⚠️ 与下架不是一回事：**撤回是降级，下架是否决**。技能撤回后在原部门仍然可用；
 * 下架之后谁都装不了。撤回是主管的工具，下架是管理部门的。
 */
export function withdrawSkill (skillId: number) {
  return http.post<{ id: number, slug: string, scope: string }>(
    '/admin/skill-hub/withdraw', { skill_id: skillId },
  )
}
