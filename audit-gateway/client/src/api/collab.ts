/**
 * 协作项目 —— 对应 server/router/v1/projects.py，**只读**
 *
 * ## 这里只有读
 *
 * 2026-09-07 起后台的协作页是看板：建项目、发言、建任务、认领开工、拉 agent
 * 全在客户端。写接口的包装**一并删掉**而不是留着不调 —— 留着的话下一个人会
 * 顺手用回来，而「后台也能写」正是这次要去掉的东西。要恢复的话，服务端那些
 * 端点一直都在，照着 `fetch*` 的形状加回来就是。
 *
 * ## 为什么调的是 `/v1/*` 而不是 `/admin/*`
 *
 * 这套端点是给客户端（桌面端 sidecar）设计的，用 API Key 认证。后台页能直接调，
 * 是因为网关的 `require_caller` **同时接受 JWT**，而 `http.ts` 带
 * `credentials: 'include'` —— cookie 里的 access_token 就是那把 JWT。
 *
 * 所以**没有**、也不需要一套 /admin/projects 端点。多开一套只会带来两份权限
 * 判定，而它们会各自漂移；漂移的表现是「桌面端能发、后台发不了」，或者更糟，
 * 反过来。
 *
 * ## 页面只对成员可见，判定在服务端
 *
 * 「不是成员」和「项目不存在」回同一个 404 —— 区分开等于告诉外人「有这么个项目，
 * 只是你进不去」。所以前端不做任何预判，拿到 404 就当没有。
 *
 * ## ⚠️ claim 与 confirm 那条规矩现在住在客户端
 *
 * 「认领返回的原文必须呈给人看、拿到确认之后才调 confirm」是 P0-6 的人在环闸门。
 * 它跟着认领动作一起搬到了桌面端（`apps/desktop/src/app/collab/`），
 * 不在这里了 —— 后台点确认时，那台机器上并没有随后要执行任务的上下文，
 * 闸门与它守着的东西分在两处，闸门就只是一次仪式。
 */

import type {
  ProjectMember,
  ProjectMessage,
  ProjectRow,
  ProjectTask,
} from '@/types/api'
import { httpV1 } from '@/api/http'

/** 我参与的项目。已退出的不返回 —— 退群解决的就是可见性 */
export function fetchMyProjects () {
  return httpV1.get<{ projects: ProjectRow[] }>('/v1/projects')
}

export function fetchMembers (code: string) {
  return httpV1.get<{ members: ProjectMember[] }>(`/v1/projects/${encodeURIComponent(code)}/members`)
}

/** 按项目内序号往后拉。用全局 id 当游标会因别的项目的写入而跳号 */
export function fetchMessages (code: string, afterSeq = 0, limit = 50) {
  return httpV1.get<{ messages: ProjectMessage[] }>(
    `/v1/projects/${encodeURIComponent(code)}/messages`, { after_seq: afterSeq, limit },
  )
}

export function fetchTasks (code: string, status = '') {
  return httpV1.get<{ tasks: ProjectTask[] }>(
    `/v1/projects/${encodeURIComponent(code)}/tasks`, status ? { status } : {},
  )
}
