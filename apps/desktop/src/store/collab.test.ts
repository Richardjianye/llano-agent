import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { CollabSyncResponse } from '@/types/hermes'

/**
 * 项目协作 store（阶段 27-C）。
 *
 * 守四条**不会报错**的规则：水位只前进且每项目一份、旧响应不覆盖新项目、
 * 读失败不清空已有内容、轮询有终止条件。
 *
 * 这四条错了都不抛异常，只会表现成「切个项目消息乱了」「聊天记录突然空了」
 * 「退出之后还在敲网关」——全都要等人发现。
 */

const api = vi.hoisted(() => ({
  getCollabTurns: vi.fn(),
  setCollabRespondTo: vi.fn(),
  tickCollabAgent: vi.fn(),
  claimCollabTask: vi.fn(),
  confirmCollabTask: vi.fn(),
  createCollabTask: vi.fn(),
  getCollabMembers: vi.fn(),
  getCollabProjects: vi.fn(),
  getCollabTasks: vi.fn(),
  resetCollabSession: vi.fn(),
  sendCollabMessage: vi.fn(),
  syncCollabMessages: vi.fn()
}))

vi.mock('@/hermes', () => api)

const msg = (seq: number, over: Record<string, unknown> = {}) => ({
  content: `第 ${seq} 条`,
  id: 1000 + seq,
  project_id: 1,
  sender_kind: 'user',
  sender_uid: 'uid-a',
  sender_username: '阿甲',
  seq,
  ...over
})

const sync = (over: Partial<CollabSyncResponse> = {}): CollabSyncResponse => ({
  messages: [],
  next_seq: 0,
  stopped: false,
  retry_after: 3600,
  ...over
})

async function freshStore() {
  vi.resetModules()

  return import('./collab')
}

/** 让轮询跑完一轮：selectCollabProject 会立刻发起第一次拉取。 */
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  vi.clearAllMocks()
  api.getCollabProjects.mockResolvedValue({ authenticated: true, projects: [], stale: false })
  api.getCollabMembers.mockResolvedValue({ members: [], stale: false })
  api.getCollabTasks.mockResolvedValue({ stale: false, tasks: [] })
  api.resetCollabSession.mockResolvedValue({ epoch: 1, ok: true })
  api.syncCollabMessages.mockResolvedValue(sync())
  api.createCollabTask.mockResolvedValue({ id: 5, seq: 1, status: 'open', title: '对账', untrusted: true })
  api.claimCollabTask.mockResolvedValue({
    claimed: true,
    confirmation: { body: '任务原文', title: '对账', untrusted: true },
    status: 'claimed'
  })
  api.confirmCollabTask.mockResolvedValue({ status: 'active', untrusted: true })
  api.getCollabTurns.mockResolvedValue({ turns: [], hops: 0, hop_limit: 8 })
  api.setCollabRespondTo.mockResolvedValue({ respond_to: 'nobody', allowlist: [] })
  api.tickCollabAgent.mockResolvedValue({ ran: false, reason: 'no_trigger' })
})

describe('项目列表', () => {
  it('基线：拉到的项目进 store', async () => {
    const store = await freshStore()

    api.getCollabProjects.mockResolvedValue({
      authenticated: true,
      projects: [{ code: 'demo', name: '示例' }],
      stale: false
    })
    await store.loadCollabProjects()

    expect(store.$collabProjects.get()).toHaveLength(1)
  })

  it('拉取失败不清空已有列表', async () => {
    // 群聊列表拉不出来不该白屏，更不该把已经显示的内容清掉。
    const store = await freshStore()

    api.getCollabProjects.mockResolvedValue({
      authenticated: true,
      projects: [{ code: 'demo' }],
      stale: false
    })
    await store.loadCollabProjects()

    api.getCollabProjects.mockRejectedValue(new Error('网关连不上'))
    // 2026-09-07 起读失败会抛：调用方要能分辨「读到了空」与「没读到」，
    // 而这两个状态在界面上必须长得不一样（见 $collabProjectsLoaded）。
    await expect(store.loadCollabProjects()).rejects.toThrow()

    expect(store.$collabProjects.get()).toEqual([{ code: 'demo' }])
    expect(store.$collabProjectsStale.get()).toBe(true)
    // 已经成功读过一次，这个标记不该被一次失败抹掉 —— 抹掉的话界面会把
    // 「暂时取不到」降级成「你没有项目」。
    expect(store.$collabProjectsLoaded.get()).toBe(true)
  })

  it('一次都没读成功时，loaded 保持 false', async () => {
    // 这是「你还没有加入任何项目」那句话能不能说的唯一依据。
    const store = await freshStore()

    api.getCollabProjects.mockRejectedValue(new Error('sidecar 还没起来'))
    await expect(store.loadCollabProjects()).rejects.toThrow()

    expect(store.$collabProjectsLoaded.get()).toBe(false)
    expect(store.$collabProjectsStale.get()).toBe(true)
  })
})

describe('增量拉取与水位', () => {
  it('新消息进 store，水位推到 next_seq', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(1), msg(2)], next_seq: 2 }))
    store.selectCollabProject('demo')
    await settle()

    expect(store.$collabState.get().demo?.messages.map(m => m.seq)).toEqual([1, 2])
    expect(store.$collabState.get().demo?.nextSeq).toBe(2)
    store.stopCollabPolling()
  })

  it('重复投递的同一条不会显示两遍', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(1), msg(1)], next_seq: 1 }))
    store.selectCollabProject('demo')
    await settle()

    expect(store.$collabState.get().demo?.messages).toHaveLength(1)
    store.stopCollabPolling()
  })

  it('水位只前进：晚到的小 next_seq 不把它拉回去', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(9)], next_seq: 9 }))
    store.selectCollabProject('demo')
    await settle()

    api.syncCollabMessages.mockResolvedValue(sync({ next_seq: 2 }))
    store.startCollabPolling('demo')
    await settle()

    expect(store.$collabState.get().demo?.nextSeq).toBe(9)
    store.stopCollabPolling()
  })

  it('每个项目一份水位', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockImplementation((code: string) =>
      Promise.resolve(
        code === 'a' ? sync({ messages: [msg(5)], next_seq: 5 }) : sync({ messages: [msg(1)], next_seq: 1 })
      )
    )

    store.selectCollabProject('a')
    await settle()
    store.selectCollabProject('b')
    await settle()

    expect(store.$collabState.get().a?.nextSeq).toBe(5)
    expect(store.$collabState.get().b?.nextSeq).toBe(1)
    store.stopCollabPolling()
  })
})

describe('旧响应不覆盖新项目', () => {
  it('切走之后回来的响应被丢弃', async () => {
    const store = await freshStore()

    let releaseSlow: (value: CollabSyncResponse) => void = () => {}

    api.syncCollabMessages.mockImplementation((code: string) => {
      if (code === 'slow') {
        return new Promise<CollabSyncResponse>(resolve => {
          releaseSlow = resolve
        })
      }

      return Promise.resolve(sync({ messages: [msg(1)], next_seq: 1 }))
    })

    store.selectCollabProject('slow')
    await settle()
    // 用户等不及，切到另一个项目。
    store.selectCollabProject('fast')
    await settle()

    // 上一个项目的响应现在才回来。
    releaseSlow(sync({ messages: [msg(99)], next_seq: 99 }))
    await settle()

    expect(store.$collabState.get().slow?.messages ?? []).toHaveLength(0)
    expect(store.$collabState.get().fast?.messages.map(m => m.seq)).toEqual([1])
    store.stopCollabPolling()
  })
})

describe('终止条件', () => {
  it('失权之后停止轮询', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ stopped: true, reason: 'revoked' }))
    store.selectCollabProject('demo')
    await settle()

    expect(store.$collabState.get().demo?.stoppedReason).toBe('revoked')

    // 停了就不再发请求 —— 没有这一条就是一个谁都看不见的后台死循环。
    const callsAfterStop = api.syncCollabMessages.mock.calls.length

    await settle()
    expect(api.syncCollabMessages.mock.calls.length).toBe(callsAfterStop)
  })

  it('退出登录也停，并把登录态标掉', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ stopped: true, reason: 'signed-out' }))
    store.selectCollabProject('demo')
    await settle()

    expect(store.$collabAuthenticated.get()).toBe(false)
  })

  it('网络抖动保留已有消息，只标 stale', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(3)], next_seq: 3 }))
    store.selectCollabProject('demo')
    await settle()
    store.stopCollabPolling()

    api.syncCollabMessages.mockRejectedValue(new Error('timeout'))
    store.startCollabPolling('demo')
    await settle()

    expect(store.$collabState.get().demo?.messages.map(m => m.seq)).toEqual([3])
    expect(store.$collabState.get().demo?.stale).toBe(true)
    store.stopCollabPolling()
  })
})

describe('账号切换清场', () => {
  it('清空所有项目状态并通知 sidecar', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(1)], next_seq: 1 }))
    store.selectCollabProject('demo')
    await settle()

    await store.resetCollab()

    expect(store.$collabState.get()).toEqual({})
    expect(store.$collabSelected.get()).toBe('')
    expect(store.$collabUnread.get()).toEqual({})
    // 两条清场路径都要有：这条防「Key 还没换但人点了退出」。
    expect(api.resetCollabSession).toHaveBeenCalled()
  })

  it('清场期间飞在路上的响应作废', async () => {
    const store = await freshStore()

    let release: (value: CollabSyncResponse) => void = () => {}

    api.syncCollabMessages.mockReturnValue(
      new Promise<CollabSyncResponse>(resolve => {
        release = resolve
      })
    )
    store.selectCollabProject('demo')
    await settle()

    await store.resetCollab()
    release(sync({ messages: [msg(7)], next_seq: 7 }))
    await settle()

    expect(store.$collabState.get()).toEqual({})
  })
})

describe('未读', () => {
  it('只在没选中这个项目时累加，打开即清零', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(1), msg(2)], next_seq: 2 }))
    store.selectCollabProject('demo')
    await settle()
    store.stopCollabPolling()

    // 切到别的项目后，demo 再来新消息。
    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(3)], next_seq: 3 }))
    store.selectCollabProject('other')
    await settle()
    store.startCollabPolling('demo')
    await settle()

    expect(store.$collabUnread.get().demo).toBe(1)

    store.selectCollabProject('demo')
    expect(store.$collabUnread.get().demo).toBe(0)
    store.stopCollabPolling()
  })
})

describe('发送', () => {
  it('失败抛出去，不伪装成已发送', async () => {
    const store = await freshStore()

    api.sendCollabMessage.mockRejectedValue(new Error('你在这个项目里是只读成员，不能发言'))

    await expect(store.sendCollab('demo', '在吗')).rejects.toThrow('只读成员')
    expect(store.$collabState.get().demo?.messages ?? []).toHaveLength(0)
  })

  it('发送不本地插入 —— 等轮询把服务端那份带回来，且只出现一次', async () => {
    // 写回响应里没有正文（投递出去的是**服务端脱敏后**的文本）。把它当一条消息
    // 插进列表，界面上就是一条空记录 —— 真机上就是这么表现的。
    const store = await freshStore()

    api.sendCollabMessage.mockResolvedValue({ id: 1004, seq: 4, redacted: true })
    await store.sendCollab('demo', '这里有LLANO Agent三个字')

    expect(store.$collabState.get().demo?.messages ?? []).toHaveLength(0)

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(4, { redaction_hits: 1 })], next_seq: 4 }))
    store.startCollabPolling('demo')
    await settle()

    const messages = store.$collabState.get().demo?.messages ?? []

    expect(messages).toHaveLength(1)
    // 回来的那份才是别人看到的那份。
    expect(messages[0].content).toBe('第 4 条')
    store.stopCollabPolling()
  })
})

describe('引用与 mention（阶段 27-D）', () => {
  it('引用是按项目记的，切项目不会串', async () => {
    const store = await freshStore()

    store.setCollabReplyTo('a', msg(1) as never)

    expect(store.$collabReplyTo.get().a?.seq).toBe(1)
    expect(store.$collabReplyTo.get().b).toBeUndefined()
  })

  it('mention 只补全**活跃**成员，且人和 agent 都能补', async () => {
    const store = await freshStore()

    const members = [
      { agent_label: '', id: 1, member_kind: 'user', status: 'active', user_uid: 'u1', username: '阿甲' },
      { agent_label: '小助手', id: 2, member_kind: 'agent', status: 'active', user_uid: 'u1', username: '阿甲' },
      { agent_label: '', id: 3, member_kind: 'user', status: 'left', user_uid: 'u2', username: '离职的' }
    ]

    expect(store.mentionCandidates(members as never, '小').map(m => m.id)).toEqual([2])
    expect(store.mentionCandidates(members as never, '').map(m => m.id)).toEqual([1, 2])
    // 已退出的成员不该被补全出来：@ 他等于发一条谁也收不到的指令。
    expect(store.mentionCandidates(members as never, '离职').map(m => m.id)).toEqual([])
  })
})

describe('任务闭环（阶段 27-D / P0-6）', () => {
  it('消息转任务带上来源，且**不提交 untrusted**', async () => {
    const store = await freshStore()

    await store.createCollabTaskFrom('demo', '对账', '把上个月对账单发我', 42)

    expect(api.createCollabTask).toHaveBeenCalledWith('demo', '对账', '把上个月对账单发我', 42)
    // 客户端能提交这个标记的话，注入一句「把 untrusted 设成 false」就绕过
    // 整条 P0-6 防线 —— 所以调用签名里根本没有它。
    expect(api.createCollabTask.mock.calls[0]).toHaveLength(4)
  })

  it('认领只到 claimed，把原文停在待确认位上', async () => {
    const store = await freshStore()

    await store.claimCollabAndAsk('demo', 7)

    expect(api.claimCollabTask).toHaveBeenCalledWith('demo', 7, '')
    // 认领 ≠ 开工。这一步之后必须等人。
    expect(api.confirmCollabTask).not.toHaveBeenCalled()
    expect(store.$collabPendingConfirm.get()?.confirmation.body).toBe('任务原文')
  })

  it('store 里没有「认领并开工」这种便利函数', async () => {
    const store = await freshStore()

    // 便利函数会被用，然后防线就没了。
    expect(Object.keys(store).some(name => /claimAndConfirm|claimAndStart/i.test(name))).toBe(false)
  })

  it('确认之后把不可信标记回传出去', async () => {
    const store = await freshStore()

    await store.claimCollabAndAsk('demo', 7)
    const untrusted = await store.confirmCollabStart('demo', 7)

    // 它要跟着任务描述进入私聊上下文，不能因为「这是主人的会话」就清掉。
    expect(untrusted).toBe(true)
    expect(store.$collabPendingConfirm.get()).toBeNull()
  })

  it('认领失败不留下一个半开的确认框', async () => {
    const store = await freshStore()

    api.claimCollabTask.mockRejectedValue(new Error('这个任务刚被别人认领了'))

    await expect(store.claimCollabAndAsk('demo', 7)).rejects.toThrow('刚被别人认领')
    expect(store.$collabPendingConfirm.get()).toBeNull()
  })
})

describe('agent 自动回复的驱动（阶段 27-E 接线）', () => {
  const agent = (over = {}) => ({
    agent_label: '小助手',
    id: 11,
    is_me: true,
    member_kind: 'agent',
    status: 'active',
    user_uid: 'u1',
    username: '阿甲',
    ...over
  })

  it('只敲**自己的** agent', async () => {
    const store = await freshStore()

    const mine = store.myAgents([
      agent(),
      agent({ id: 12, is_me: false, agent_label: '别人的' }),
      { id: 13, is_me: true, member_kind: 'user', status: 'active', user_uid: 'u1', agent_label: '' }
    ] as never)

    // 别人的 agent 不是我能替它开工的 —— 网关也会拒，但这里就不该发出去。
    expect(mine.map(m => m.id)).toEqual([11])
  })

  it('退出项目的 agent 不再被敲', async () => {
    const store = await freshStore()

    expect(store.myAgents([agent({ status: 'left' })] as never)).toEqual([])
  })

  it('tick 起来之后会替我的 agent 要 turn', async () => {
    const store = await freshStore()

    api.getCollabMembers.mockResolvedValue({ members: [agent()], stale: false })
    store.selectCollabProject('demo')
    await settle()
    await settle()

    expect(api.tickCollabAgent).toHaveBeenCalledWith('demo', 11, 0)
    store.stopCollabTicking()
    store.stopCollabPolling()
  })

  it('stopped 之后不再敲 —— 每隔几秒换来同一句「不行」没有意义', async () => {
    const store = await freshStore()

    api.getCollabMembers.mockResolvedValue({ members: [agent()], stale: false })
    api.tickCollabAgent.mockResolvedValue({ ran: false, stopped: true, reason: 'hop_limit' })
    store.selectCollabProject('demo')
    await settle()
    await settle()

    const calls = api.tickCollabAgent.mock.calls.length

    await settle()
    expect(api.tickCollabAgent.mock.calls.length).toBe(calls)
    store.stopCollabPolling()
  })

  it('切项目/清场会停掉上一个项目的 tick', async () => {
    const store = await freshStore()

    api.getCollabMembers.mockResolvedValue({ members: [agent()], stale: false })
    store.selectCollabProject('demo')
    await settle()

    await store.resetCollab()
    const calls = api.tickCollabAgent.mock.calls.length

    await settle()
    await settle()
    // 清场之后一次都不该再敲：每一轮 tick 都可能变成一次真实的模型调用。
    expect(api.tickCollabAgent.mock.calls.length).toBe(calls)
  })

  it('改触发范围之后重新拉成员，界面才不会显示旧值', async () => {
    const store = await freshStore()

    await store.updateRespondTo('demo', 11, 'nobody')

    expect(api.setCollabRespondTo).toHaveBeenCalledWith('demo', 11, 'nobody', [])
    expect(api.getCollabMembers).toHaveBeenCalled()
  })

  it('改触发范围失败要抛出去 —— 静默失败等于「你以为关了其实没关」', async () => {
    const store = await freshStore()

    api.setCollabRespondTo.mockRejectedValue(new Error('触发范围只能是 …'))

    await expect(store.updateRespondTo('demo', 11, 'everyone')).rejects.toThrow('触发范围')
  })
})

describe('自己刚说完话要立刻拉一次', () => {
  /**
   * 空闲退避会爬到 8 秒，而自己刚说完话是最不能等的那一刻：发出去的那条**不会**
   * 被本地插入（写回响应里没有正文，投递出去的是服务端脱敏后的那份），要等下一轮
   * sync 才回来。于是别人先看到我说的话、我自己后看到 —— 2026-09-07 真机上就是
   * 这么表现的。
   */
  it('发完消息立刻再拉一次，不等退避', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [], next_seq: 0, retry_after: 8 }))
    store.selectCollabProject('demo')
    await settle()

    const before = api.syncCollabMessages.mock.calls.length

    api.sendCollabMessage.mockResolvedValue({ id: 1, seq: 1 })
    api.syncCollabMessages.mockResolvedValue(sync({ messages: [msg(1)], next_seq: 1, retry_after: 8 }))
    await store.sendCollab('demo', '在吗')
    await settle()

    expect(api.syncCollabMessages.mock.calls.length).toBeGreaterThan(before)
    expect(store.$collabState.get().demo?.messages.map(m => m.seq)).toEqual([1])
    store.stopCollabPolling()
  })

  it('发送**仍然不**本地插入 —— 写回响应里没有正文', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [], next_seq: 0 }))
    store.selectCollabProject('demo')
    await settle()

    // 把它当成一条消息插进列表，界面上就是一条空记录（真机上就是这么表现的）。
    // 提前拉一次是为了让服务端那份**更快**回来，不是绕开它。
    api.sendCollabMessage.mockResolvedValue({ id: 1, redacted: false, seq: 1 })
    api.syncCollabMessages.mockResolvedValue(sync({ messages: [], next_seq: 0 }))
    await store.sendCollab('demo', '在吗')
    await settle()

    expect(store.$collabState.get().demo?.messages).toEqual([])
    store.stopCollabPolling()
  })

  it('切走的项目叫不醒 —— 不给已经不看的项目发请求', async () => {
    const store = await freshStore()

    api.syncCollabMessages.mockResolvedValue(sync({ messages: [], next_seq: 0, retry_after: 8 }))
    store.selectCollabProject('demo')
    await settle()
    store.stopCollabPolling()

    const before = api.syncCollabMessages.mock.calls.length

    store.pokeCollabPolling('demo')
    await settle()

    expect(api.syncCollabMessages.mock.calls.length).toBe(before)
  })
})

describe('quoted reply mention drafts', () => {
  it('mentions the sender and preserves draft text when switching or cancelling a quote', async () => {
    const store = await freshStore()
    const human = { ...msg(1), sender_kind: 'user', sender_username: '阿甲' } as never
    const agent = {
      ...msg(2),
      sender_kind: 'agent',
      sender_username: '阿乙',
      sender_agent_label: '乙的测试助手'
    } as never
    store.$collabDrafts.setKey('a', '补充说明')
    store.setCollabReplyTo('a', human)
    expect(store.$collabDrafts.get().a).toBe('@阿甲 补充说明')
    store.setCollabReplyTo('a', human)
    expect(store.$collabDrafts.get().a).toBe('@阿甲 补充说明')
    store.setCollabReplyTo('a', agent)
    expect(store.$collabDrafts.get().a).toBe('@乙的测试助手 补充说明')
    expect(store.$collabDrafts.get().b).toBeUndefined()
    store.setCollabReplyTo('a', null)
    expect(store.$collabDrafts.get().a).toBe('补充说明')
  })
  it('does not overwrite a manually edited mention when cancelling', async () => {
    const store = await freshStore()
    store.setCollabReplyTo('a', { ...msg(1), sender_kind: 'user', sender_username: '阿甲' } as never)
    store.$collabDrafts.setKey('a', '@阿乙 手动修改')
    store.setCollabReplyTo('a', null)
    expect(store.$collabDrafts.get().a).toBe('@阿乙 手动修改')
  })
})
