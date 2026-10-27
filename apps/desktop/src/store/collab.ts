import { atom, map } from 'nanostores'

import {
  claimCollabTask,
  confirmCollabTask,
  createCollabProject,
  createCollabTask,
  getCollabDirectory,
  getCollabMembers,
  getCollabMessages,
  getCollabProjects,
  getCollabTasks,
  getCollabTurns,
  inviteCollabMember,
  pullMyCollabAgent,
  resetCollabSession,
  sendCollabMessage,
  setCollabRespondTo,
  syncCollabMessages,
  tickCollabAgent
} from '@/hermes'
import type {
  CollabAvailability,
  CollabClaimResponse,
  CollabDirectoryUser,
  CollabMember,
  CollabMessage,
  CollabPendingReply,
  CollabProject,
  CollabTask,
  CollabTurn
} from '@/types/hermes'

import { $collabRuntimeActive } from './collab-runtime'

/**
 * 项目协作 —— **独立于私聊的**状态原子（阶段 27-C）。
 *
 * ## 为什么不复用 session store
 *
 * 项目群聊看起来像另一个聊天页，但它的每一样东西都不同：数据源是网关而不是本地
 * 会话，未读是项目级的，恢复水位是项目内 `seq`，参与者里有别人的 agent，而且
 * 群聊上下文**不持有个人工具、记忆与凭据**（决策 #45）。
 *
 * 复用 session store 的代价不是重复代码，是**串线**：一个项目会话混进 Recents
 * 之后，它就会被会话恢复、模型切换、个人 skill 注入这些逻辑当成普通私聊处理，
 * 而这些逻辑没有一处知道「这条上下文来自群里的别人」。所以这里从 route、store
 * 到发送动作全部独立，只共享纯展示组件。
 *
 * ## 三条不会报错的规则
 *
 * 1. **水位只前进**，且每个项目一份。用全局游标的话，切个项目就会漏消息。
 * 2. **旧响应不覆盖新项目**：每次切项目/清场 `generation` 加一，回来的响应对不上
 *    就丢弃。没有这条的话，慢响应会把用户刚切过去的项目内容盖掉。
 * 3. **读失败不清空已有内容**。sidecar 会回上一次的缓存并标 `stale`，这里也不能
 *    因为一次失败就把消息列表设成空 —— 那看起来像「聊天记录没了」。
 *
 * ## 轮询有终止条件
 *
 * P0 的实时通路是 `after_seq` 增量拉取（阶段 27 实施决议 1）。`stopped` 一到就
 * 停：退出登录、项目归档、被移出成员、账号停用都走这条。离开页面同样停 ——
 * 不能留下一个后台死循环。
 */

export interface CollabProjectState {
  pending: CollabPendingReply[]
  availability: CollabAvailability[]
  agentError: string
  turnsStale: boolean
  error: string
  loading: boolean
  members: CollabMember[]
  messages: CollabMessage[]
  nextSeq: number
  /** 为什么停了：signed-out / revoked / reset。空串表示没停。 */
  stoppedReason: string
  /** 上一次读失败、正在用缓存内容。界面据此显示离线条，而不是清空。 */
  stale: boolean
  tasks: CollabTask[]
  /** agent 活跃状态与当前跳数，供界面显示「谁在说话」「为什么停了」。 */
  turns: CollabTurn[]
  hops: number
  hopLimit: number
}

const EMPTY_PROJECT: CollabProjectState = {
  pending: [],
  availability: [],
  agentError: '',
  turnsStale: false,
  error: '',
  loading: false,
  members: [],
  messages: [],
  nextSeq: 0,
  stoppedReason: '',
  stale: false,
  tasks: [],
  turns: [],
  hops: 0,
  hopLimit: 8
}

/** 一个项目最多在内存里留多少条消息。再往上翻要回网关拿 —— 内存里存一整段
 *  历史既没必要，也会让长期开着的窗口越来越重。 */
const MAX_MESSAGES_IN_MEMORY = 500

export const $collabAccountEpoch = atom(0)
export const $collabVisible = atom(false)
export const $collabProjects = atom<CollabProject[]>([])
export const $collabProjectsStale = atom(false)
/**
 * 项目列表**成功读到过一次**没有。
 *
 * 没有这个标记的话，「读失败」和「你一个项目都没加入」在界面上是同一个状态 ——
 * 而 2026-09-07 真机上就是这么表现的：页面在 sidecar 起来之前先发了一次请求，
 * 那一次抛了，于是界面同时显示「你还没有加入任何项目」和「暂时取不到最新消息」。
 * 前半句是一个**我们根本没拿到数据**的断言，比不显示更糟。
 */
export const $collabProjectsLoaded = atom(false)
export const $collabAuthenticated = atom(true)
export const $collabSelected = atom<string>('')
/** 每个项目一条状态。key 是项目 code。 */
export const $collabState = map<Record<string, CollabProjectState>>({})
/** 项目级未读数。**不进私聊 Recents**，也不与会话未读共用任何一个原子。 */
export const $collabUnread = map<Record<string, number>>({})

let generation = 0
let pollTimer: null | ReturnType<typeof setTimeout> = null
let pollingCode = ''
/** 把正在等退避的那一轮提前叫醒。由 `startCollabPolling` 装上。 */
let pollWake: (() => void) | null = null

function stateOf(code: string): CollabProjectState {
  return $collabState.get()[code] ?? EMPTY_PROJECT
}

function patch(code: string, next: Partial<CollabProjectState>): void {
  $collabState.setKey(code, { ...stateOf(code), ...next })
}

/** 合并新消息：按 seq 去重并排序。sidecar 已经去过一次重，这里再挡一次是因为
 *  「自己刚发的那条」与轮询回来的那条会在界面上撞车。 */
function mergeMessages(existing: CollabMessage[], incoming: CollabMessage[]): CollabMessage[] {
  if (incoming.length === 0) {
    return existing
  }

  const bySeq = new Map<number, CollabMessage>()

  for (const message of existing) {
    bySeq.set(message.seq, message)
  }

  for (const message of incoming) {
    bySeq.set(message.seq, message)
  }

  const merged = [...bySeq.values()].sort((a, b) => a.seq - b.seq)

  return merged.length > MAX_MESSAGES_IN_MEMORY ? merged.slice(-MAX_MESSAGES_IN_MEMORY) : merged
}

export async function loadCollabProjects(): Promise<void> {
  const mine = $collabAccountEpoch.get()

  try {
    const response = await getCollabProjects()

    if (mine !== $collabAccountEpoch.get()) {
      return
    }

    $collabAuthenticated.set(response.authenticated)
    $collabProjectsStale.set(Boolean(response.stale))
    // 失败时 sidecar 回的是缓存，所以这里照单接收即可 —— 不做「空了就清空」。
    $collabProjects.set(response.projects ?? [])
    $collabProjectsLoaded.set(true)
  } catch {
    // 列表拉不出来不该让整个页面白屏，更不该把已有列表清掉 ——
    // 也**不该冒充成「你没有项目」**，见 $collabProjectsLoaded。
    if (mine === $collabAccountEpoch.get()) {
      $collabProjectsStale.set(true)
    }

    throw new Error('collab-projects-unavailable')
  }
}

/** 列表读不到时的重试节奏。停在 8 秒：再慢下去与「坏了」没有区别。 */
const PROJECT_RETRY_STEPS_MS = [1_000, 2_000, 4_000, 8_000]

/**
 * 一直重试到读出来为止。返回一个停止函数，页面卸载时必须调。
 *
 * 只在挂载时读一次是不够的：sidecar 是被主进程拉起来的，页面完全可能比它先到。
 * 那一次失败之后没有任何东西会再试，于是这一页在整个会话里都停在「取不到」——
 * 除非用户自己想到切走再切回来。
 */
export function startCollabProjectsLoad(): () => void {
  let stopped = false
  let timer: null | ReturnType<typeof setTimeout> = null
  let attempt = 0

  const tick = async () => {
    if (stopped) {
      return
    }

    try {
      await loadCollabProjects()
    } catch {
      if (stopped) {
        return
      }

      const wait = PROJECT_RETRY_STEPS_MS[Math.min(attempt, PROJECT_RETRY_STEPS_MS.length - 1)]

      attempt += 1
      timer = setTimeout(() => void tick(), wait)
    }
  }

  void tick()

  return () => {
    stopped = true

    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }
}

/**
 * 建项目，建完直接切过去。
 *
 * 建项目原本只有后台「协作」页有 —— 而后台的定位是看板。协作发生在客户端，
 * 建项目也该在客户端（2026-09-07）。
 *
 * 失败**抛出去**：写路径失败必须让调用方显示出来，不能静默留在原地让人以为建好了。
 */
export async function createCollabProjectAndOpen(code: string, name = '', description = ''): Promise<string> {
  const created = await createCollabProject(code, name, description)
  const actual = created.code || code

  // 项目**已经建好了**。刷新列表失败不能倒过来变成「建项目失败」——
  // 那会让人以为要重建一个，而重建会撞「标识已被占用」。
  await loadCollabProjects().catch(() => undefined)
  selectCollabProject(actual)

  return actual
}

/**
 * 搜通讯录。读路径：拿不到回空数组，让「加成员」那个框显示「没找到」而不是炸掉。
 */
export async function searchCollabDirectory(query = ''): Promise<CollabDirectoryUser[]> {
  try {
    return (await getCollabDirectory(query)).users ?? []
  } catch {
    return []
  }
}

/**
 * 把一个人加进项目。**只有负责人能加**，判定在服务端 —— 这里不预判，
 * 让网关那句话原样传上去。
 */
export async function inviteCollabPerson(code: string, uid: string, username = ''): Promise<void> {
  await inviteCollabMember(code, uid, username)
  await loadCollabSidebars(code)
}

/**
 * 把自己的 agent 拉进当前项目。
 *
 * 没有这一步，桌面端建出来的项目里一个 agent 都没有 —— 而 agent 在群里说话正是
 * 这个功能存在的理由。**只能拉自己的**，服务端在 agent 分支一律用调用者的 uid。
 */
export async function pullMyAgentInto(code: string, agentLabel = ''): Promise<void> {
  await pullMyCollabAgent(code, agentLabel)
  await loadCollabSidebars(code)
}

/**
 * 打开项目时先把历史铺出来。
 *
 * 只靠 `/sync` 的话，页面一重开就是空的：水位在 sidecar 进程里且已经推到最后，
 * 而这边的内存随刷新没了 —— 表现为「聊天记录不见了，等有人再说一句才冒出来」。
 * 历史读是无状态的，增量拉取才有水位，两件事分开。
 */
export async function loadCollabHistory(code: string): Promise<void> {
  const mine = generation

  try {
    const latest = Math.max(stateOf(code).nextSeq, $collabProjects.get().find(p => p.code === code)?.message_seq || 0)
    const response = await getCollabMessages(code, latest > 200 ? latest - 200 : undefined)

    if (mine !== generation || $collabSelected.get() !== code) {
      return
    }

    const messages = response.messages ?? []

    patch(code, {
      loading: false,
      messages: mergeMessages(stateOf(code).messages, messages),
      // 水位跟着历史走：下一轮增量从这里往后拉，不会把刚铺出来的再拉一遍。
      nextSeq: Math.max(stateOf(code).nextSeq, ...messages.map(m => m.seq), 0),
      stale: Boolean(response.stale)
    })
  } catch {
    // 历史拉不到不清空已有内容 —— 与读路径其它地方同一条规矩。
    if (mine === generation) {
      patch(code, { loading: false, stale: true })
    }
  }
}

export async function loadCollabSidebars(code: string): Promise<void> {
  const mine = generation

  const [members, tasks] = await Promise.allSettled([getCollabMembers(code), getCollabTasks(code)])

  if (mine !== generation || $collabSelected.get() !== code) {
    return
  }

  if (members.status === 'fulfilled') {
    patch(code, { members: members.value.members ?? [] })
  }

  if (tasks.status === 'fulfilled') {
    patch(code, { tasks: tasks.value.tasks ?? [] })
  }
}

/**
 * 切到某个项目。**这是唯一会推进 `generation` 的入口之一** —— 切换之后，上一个
 * 项目还飞在路上的响应全部作废。
 */
export function selectCollabProject(code: string): void {
  if ($collabSelected.get() === code) {
    return
  }

  stopCollabPolling()
  stopCollabTicking()
  generation += 1
  $collabSelected.set(code)

  if (!code) {
    return
  }

  if (!$collabState.get()[code]) {
    $collabState.setKey(code, { ...EMPTY_PROJECT })
  }

  // 打开即已读。未读只在**没选中**这个项目时累加。
  $collabUnread.setKey(code, 0)
  // 先铺历史，再开增量 —— 反过来的话第一轮增量会从 0 拉一遍，然后历史又盖上去。
  void loadCollabHistory(code).finally(() => {
    if (!$collabRuntimeActive.get() && $collabSelected.get() === code) {
      startCollabPolling(code)
    }
  })
  void loadCollabTurns(code)
  // 成员列表回来之后才知道哪个 agent 是我的，所以 tick 挂在它后面。
  void loadCollabSidebars(code).finally(() => {
    if (!$collabRuntimeActive.get() && $collabSelected.get() === code) {
      startCollabTicking(code)
    }
  })
}

async function pollOnce(code: string, mine: number): Promise<number> {
  const state = stateOf(code)

  try {
    const response = await syncCollabMessages(code, state.nextSeq)

    if (mine !== generation) {
      // 清场/切项目期间回来的旧响应。丢掉 —— 采纳它就会把新项目的内容盖掉。
      return 0
    }

    if (response.stopped) {
      patch(code, { stoppedReason: response.reason || 'stopped', loading: false })

      if (response.reason === 'signed-out') {
        $collabAuthenticated.set(false)
      }

      return 0
    }

    void loadCollabTurns(code)
    const fresh = response.messages ?? []

    patch(code, {
      error: '',
      loading: false,
      messages: mergeMessages(stateOf(code).messages, fresh),
      nextSeq: Math.max(stateOf(code).nextSeq, response.next_seq ?? 0),
      stale: Boolean(response.offline)
    })

    if (fresh.length > 0 && $collabSelected.get() !== code) {
      $collabUnread.setKey(code, ($collabUnread.get()[code] ?? 0) + fresh.length)
    }

    return Math.max(1, (response.retry_after ?? 2) * 1000)
  } catch (error) {
    if (mine !== generation) {
      return 0
    }

    // 抖动：保留已有消息，标离线，退避后再来。清空内容是这里最不该做的事。
    patch(code, { loading: false, stale: true, error: error instanceof Error ? error.message : String(error) })

    return 8_000
  }
}

export function startCollabPolling(code: string): void {
  stopCollabPolling()

  if (!code) {
    return
  }

  pollingCode = code
  const mine = generation
  // 这两个跟着**这一轮循环**走，不是模块级的：切项目时上一个项目的 sync 可能还
  // 在飞，模块级的话新项目会被上一个的飞行状态挡住，一直等到它落地才开始拉。
  let inFlight = false
  let poked = false

  const tick = async () => {
    if (mine !== generation || pollingCode !== code) {
      return
    }

    if (inFlight) {
      poked = true

      return
    }

    inFlight = true
    poked = false

    let delay: number

    try {
      delay = await pollOnce(code, mine)
    } finally {
      inFlight = false
    }

    if (delay <= 0 || mine !== generation || pollingCode !== code) {
      // stopped（失权/退出）或已经切走：不再安排下一轮。
      return
    }

    // 飞行途中被叫醒过：那次 sync 出发得比我们发的消息还早，很可能没带回来。
    pollTimer = setTimeout(() => void tick(), poked ? 0 : delay)
  }

  pollWake = () => {
    if (pollTimer) {
      clearTimeout(pollTimer)
      pollTimer = null
    }

    void tick()
  }

  patch(code, { loading: stateOf(code).messages.length === 0 })
  void tick()
}

/**
 * 立刻拉一次，不等退避。
 *
 * 空闲时退避会爬到 8 秒，而**自己刚说完话**是最不能等的那一刻：发出去的那条要
 * 等下一轮 sync 才回来（写回响应里没有正文，见 `sendCollab`），于是别人先看到
 * 我说的话、我自己后看到。2026-09-07 真机上就是这么表现的。
 *
 * 只是把下一轮提前，不是额外加一轮：已经在飞的那次不会被叠上第二条。
 */
export function pokeCollabPolling(code: string): void {
  if (pollingCode === code && pollWake) {
    pollWake()
  }
}

/** 停止轮询。页面离开、账号退出、项目失权、应用关闭都必须走到这里。 */
export function stopCollabPolling(): void {
  pollWake = null

  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }

  pollingCode = ''
}

/**
 * 账号退出/切换时清场。
 *
 * sidecar 那边也会按身份指纹自动清（它不信界面记得调这条），两边都要有：这条
 * 防「Key 还没换但人已经点了退出」，那条防「界面忘了调」。
 */
export async function resetCollab(): Promise<void> {
  $collabAccountEpoch.set($collabAccountEpoch.get() + 1)
  stopCollabPolling()
  stopCollabTicking()
  generation += 1
  $collabProjects.set([])
  $collabProjectsStale.set(false)
  $collabProjectsLoaded.set(false)
  $collabSelected.set('')
  $collabState.set({})
  $collabUnread.set({})
  $collabReplyTo.set({})
  $collabDrafts.set({})
  $collabPendingConfirm.set(null)

  try {
    await resetCollabSession()
  } catch {
    // 清场是本地状态的事，网关那边清不掉也不该让界面卡住。
  }
}

/**
 * 发一条群聊消息。
 *
 * 失败**抛出去**：用户按了发送却什么都没发生，必须说清楚为什么。这里不做乐观
 * 插入 —— 乐观插入在失败时要么留下一条假消息，要么把它撤掉（而撤掉的动画看起来
 * 就像消息丢了）。服务端还会脱敏，乐观显示的那份未必是别人看到的那份。
 */
export async function sendCollab(code: string, content: string, replyToId?: null | number): Promise<CollabMessage> {
  // 写回响应只有 {id, seq, redacted, mentions} —— **没有正文**：投递出去的那份是
  // 服务端脱敏后的文本。把它当成一条消息插进列表，界面上就是一条空记录（真机
  // 上就是这么表现的）。所以这里什么都不插，等下一轮 sync 把服务端那份带回来。
  const written = await sendCollabMessage(code, content, replyToId)
  const mentions = Array.isArray(written.mentions) ? written.mentions : []

  const pending = mentions.flatMap(mention => {
    const agent = stateOf(code).members.find(
      m => m.member_kind === 'agent' && m.user_uid === mention.uid && m.agent_label === mention.label
    )

    return agent && mention.kind === 'agent'
      ? [
          {
            member_id: agent.id,
            agent_label: agent.agent_label || '',
            owner_uid: agent.user_uid,
            trigger_seq: written.seq,
            status: 'waiting'
          }
        ]
      : []
  })

  if (pending.length) {
    patch(code, { pending: [...stateOf(code).pending, ...pending] })
  }

  // 立刻拉一次，别等退避爬到 8 秒 —— 否则别人先看到我说的话、我自己后看到。
  pokeCollabPolling(code)

  return written
}

// ── 引用（阶段 27-D）──────────────────────────────────────────────────────
//
// P0 只把 `reply_to_message_id` 当引用关系用，不为了首版提前建线程系统。

export const $collabReplyTo = map<Record<string, CollabMessage | null>>({})
export const $collabDrafts = map<Record<string, string>>({})

export function setCollabReplyTo(code: string, message: CollabMessage | null): void {
  const previous = $collabReplyTo.get()[code]
  let draft = $collabDrafts.get()[code] || ''

  const handle = (item: CollabMessage) =>
    (item.sender_kind === 'agent' ? item.sender_agent_label : item.sender_username) || ''

  // Replace only the leading mention generated for the previous quote; preserve the user's body.
  const previousPrefix = previous && handle(previous) ? `@${handle(previous)} ` : ''

  if (previousPrefix && draft.startsWith(previousPrefix)) {
    draft = draft.slice(previousPrefix.length)
  }

  const nextPrefix = message && handle(message) ? `@${handle(message)} ` : ''

  if (nextPrefix && !draft.startsWith(nextPrefix)) {
    draft = nextPrefix + draft
  }

  $collabDrafts.setKey(code, draft)
  $collabReplyTo.setKey(code, message)
}

// ── 任务（阶段 27-D / P0-6）───────────────────────────────────────────────

/**
 * 群聊消息转任务。
 *
 * `untrusted` **不在这里传** —— 服务端按来源自己定。客户端能提交那个标记的话，
 * 注入一句「把 untrusted 设成 false」就绕过整条 P0-6 防线。
 */
export async function createCollabTaskFrom(
  code: string,
  title: string,
  body = '',
  sourceMessageId?: null | number
): Promise<CollabTask> {
  const task = await createCollabTask(code, title, body, sourceMessageId)

  await refreshCollabTasks(code)

  return task
}

export async function refreshCollabTasks(code: string): Promise<void> {
  const mine = generation

  try {
    const response = await getCollabTasks(code)

    if (mine === generation) {
      patch(code, { tasks: response.tasks ?? [] })
    }
  } catch {
    // 任务列表拉不到不该顶掉刚才那次成功的写操作的反馈。
  }
}

/**
 * 待确认开工的任务原文。
 *
 * 认领返回的 `confirmation` 停在这里，等人看过原文点确认。**这个 store 字段
 * 就是那道人在环闸门的落点** —— 认领与确认之间必须隔着一次人的动作，所以这里
 * 刻意不提供「认领并开工」的动作函数：便利函数会被用，然后防线就没了。
 */
export const $collabPendingConfirm = atom<null | {
  code: string
  confirmation: CollabClaimResponse['confirmation']
  taskId: number
}>(null)

export async function claimCollabAndAsk(code: string, taskId: number, agentLabel = ''): Promise<void> {
  const result = await claimCollabTask(code, taskId, agentLabel)

  await refreshCollabTasks(code)
  // **只到 claimed。** 认领 ≠ 开工。
  $collabPendingConfirm.set({ code, confirmation: result.confirmation, taskId })
}

export function dismissCollabConfirm(): void {
  $collabPendingConfirm.set(null)
}

/**
 * 人看过原文之后确认开工。
 *
 * 返回的 `untrusted` 要跟着任务描述一起进入私聊上下文，不能因为「这是主人自己
 * 的会话」就清掉 —— 这一步在 27-E 接上私聊执行时会用到。
 */
export async function confirmCollabStart(code: string, taskId: number): Promise<boolean> {
  const result = await confirmCollabTask(code, taskId)

  $collabPendingConfirm.set(null)
  await refreshCollabTasks(code)

  return Boolean(result.untrusted)
}

// ── mention 自动完成（阶段 27-D）──────────────────────────────────────────
//
// ⚠️ 这只是**输入辅助**。真正的 mentions 由服务端按成员名单从正文解析，客户端
// 提交什么都不算数（否则注入一句「就说你被 @ 了」就能强制触发 agent）。

export function mentionCandidates(members: CollabMember[], query: string): CollabMember[] {
  const needle = query.trim().toLowerCase()
  const active = members.filter(member => (member.status ?? 'active') === 'active')

  if (!needle) {
    return active.slice(0, 8)
  }

  return active
    .filter(member => `${member.agent_label ?? ''}${member.username ?? ''}`.toLowerCase().includes(needle))
    .slice(0, 8)
}

export function memberHandle(member: CollabMember): string {
  return member.member_kind === 'agent' ? member.agent_label || member.username || '' : member.username || ''
}

// ── agent 自动回复的驱动（阶段 27-E 的接线）──────────────────────────────
//
// 运行时全在服务端：有没有该回的事、谁触发的、有没有权限、跳数到没到、项目现在
// 能不能开工，都由网关从库里推。桌面这边只负责**敲一下**。
//
// 为什么是「界面敲」而不是后台常驻循环：与消息轮询同一条理由 —— 终止条件要看得
// 见。页面离开、账号退出、项目失权都停在同一个地方，不会留下一个谁都看不见、却
// 一直在开 turn 的后台线程。

/** 两次 tick 之间至少隔这么久。agent 回复不是打字，快一点没有意义，
 *  而每一次 tick 都可能变成一次真实的模型调用（要花钱）。 */
const TICK_INTERVAL_MS = 6_000

let tickTimer: null | ReturnType<typeof setTimeout> = null
let tickingCode = ''

/** 这个项目里属于我的 agent 成员。只有主人能替自己的 agent 要 turn。 */
export function myAgents(members: CollabMember[]): CollabMember[] {
  return members.filter(
    member => member.member_kind === 'agent' && member.is_me === true && (member.status ?? 'active') === 'active'
  )
}

async function tickOnce(code: string, mine: number): Promise<void> {
  const state = stateOf(code)
  const agents = myAgents(state.members)

  for (const agent of agents) {
    if (mine !== generation || tickingCode !== code) {
      return
    }

    try {
      // `after_seq=0` 是**故意**的：触发水位由服务端记（见网关的
      // `last_handled_trigger_seq`）。客户端自己记水位的话，重启就丢、开两个
      // 客户端就有两份 —— 2026-09-07 那次刷屏正是客户端水位永远停在 0 造成的。
      const result = await tickCollabAgent(code, agent.id, 0)

      if (result.stopped) {
        // 熔断到了 / 被暂停 / 项目归档。停这个项目的 tick —— 继续敲只会
        // 每隔几秒换来同一句「不行」。
        tickingCode = ''

        return
      }

      if (result.replied) {
        // 回复本身会被下一轮 sync 带回来，这里不插消息 —— 但要把那一轮提前，
        // 否则自己的 agent 说完话，自己是群里最后一个看到的。
        pokeCollabPolling(code)
        void loadCollabTurns(code)
      }
    } catch {
      // 触发失败不该让界面报错：绝大多数情况是「现在没事做」之外的临时问题，
      // 而 agent 不说话本来就是可观察的。活跃状态那一栏会显示 failed。
      void loadCollabTurns(code)
    }
  }
}

export function startCollabTicking(code: string): void {
  stopCollabTicking()

  if (!code) {
    return
  }

  tickingCode = code
  const mine = generation

  const beat = async () => {
    if (mine !== generation || tickingCode !== code) {
      return
    }

    await tickOnce(code, mine)

    if (mine !== generation || tickingCode !== code) {
      return
    }

    tickTimer = setTimeout(() => void beat(), TICK_INTERVAL_MS)
  }

  void beat()
}

export function stopCollabTicking(): void {
  if (tickTimer) {
    clearTimeout(tickTimer)
    tickTimer = null
  }

  tickingCode = ''
}

export async function loadCollabTurns(code: string): Promise<void> {
  const mine = generation

  try {
    const response = await getCollabTurns(code)

    if (mine === generation && $collabSelected.get() === code) {
      patch(code, {
        pending: response.pending ?? [],
        availability: response.availability ?? [],
        turnsStale: Boolean(response.stale),
        turns: response.turns ?? [],
        hops: response.hops ?? 0,
        hopLimit: response.hop_limit ?? 8
      })
    }
  } catch {
    // 活跃状态取不到不影响聊天本身。
  }
}

/**
 * 改这个 agent 接受谁的指令（P0-D 的配置入口）。
 *
 * 失败抛出去：权限设置静默失败等于「你以为关了，其实没关」。
 */
export async function updateRespondTo(
  code: string,
  memberId: number,
  respondTo: string,
  allowlist: string[] = []
): Promise<void> {
  await setCollabRespondTo(code, memberId, respondTo, allowlist)
  await loadCollabSidebars(code)
}

/** 测试与页面卸载用。 */
export function collabGeneration(): number {
  return generation
}

/** Background snapshots only update their own project cache. */
export function applyCollabSnapshot(code: string, update: Partial<CollabProjectState>): void {
  patch(code, {
    ...update,
    ...(update.messages ? { messages: mergeMessages(stateOf(code).messages, update.messages) } : {}),
    nextSeq: Math.max(stateOf(code).nextSeq, update.nextSeq || 0)
  })
}

/** Remove content immediately when membership is revoked; retain only a reason. */
export function forgetCollabProject(code: string, reason = 'revoked'): void {
  if ($collabPendingConfirm.get()?.code === code) {
    $collabPendingConfirm.set(null)
  }

  $collabState.setKey(code, { ...EMPTY_PROJECT, stoppedReason: reason })
  $collabDrafts.setKey(code, '')
  $collabReplyTo.setKey(code, null)
  $collabUnread.setKey(code, 0)
}
