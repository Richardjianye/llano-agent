import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

/**
 * 从全员通讯录里加成员（2026-09-07）。
 *
 * 在这之前客户端**拿不到别人的 uid** —— 后台的账号列表要 `user:view`，普通员工
 * 没有。所以「协作全部在客户端进行」缺了最后一块：项目建得出来、自己的 agent
 * 拉得进来，同事拉不进来。
 *
 * 通讯录全员可见（与钉钉一致，由负责人拍板）。这一组守的是客户端这半边：
 * 已经在项目里的人不重复列、加成员失败要说话、「只有负责人能加」不在客户端预判。
 */

const api = vi.hoisted(() => ({
  claimCollabTask: vi.fn(),
  confirmCollabTask: vi.fn(),
  createCollabProject: vi.fn(),
  createCollabTask: vi.fn(),
  getCollabDirectory: vi.fn(),
  getCollabMembers: vi.fn(),
  getCollabMessages: vi.fn(),
  getCollabProjects: vi.fn(),
  getCollabTasks: vi.fn(),
  getCollabTurns: vi.fn(),
  inviteCollabMember: vi.fn(),
  pullMyCollabAgent: vi.fn(),
  resetCollabSession: vi.fn(),
  sendCollabMessage: vi.fn(),
  setCollabRespondTo: vi.fn(),
  syncCollabMessages: vi.fn(),
  tickCollabAgent: vi.fn()
}))

vi.mock('@/hermes', () => api)
vi.mock('./task-result', () => ({ TaskResult: () => null }))

const ME = { is_me: true, id: 1, member_kind: 'user', role: 'owner', user_uid: 'u-1', username: '阿甲' }

async function mountPanel() {
  const store = await import('@/store/collab')
  const { MembersPanel } = await import('./panels')

  store.$collabState.setKey('demo', {
    hopLimit: 8,
    hops: 0,
    loading: false,
    members: [ME],
    messages: [],
    nextSeq: 0,
    stale: false,
    stoppedReason: '',
    tasks: [],
    turns: []
  } as never)

  render(
    <I18nProvider configClient={null} initialLocale="zh">
      <MembersPanel code="demo" />
    </I18nProvider>
  )

  fireEvent.click(screen.getByTestId('collab-invite-open'))
}

beforeEach(() => {
  for (const fn of Object.values(api)) {
    fn.mockReset()
  }

  api.getCollabMembers.mockResolvedValue({ members: [ME], stale: false })
  api.getCollabTasks.mockResolvedValue({ stale: false, tasks: [] })
  api.getCollabDirectory.mockResolvedValue({
    authenticated: true,
    stale: false,
    users: [
      { dept_name: '产品研发中心', realname: '协作测试甲', uid: 'u-1', username: '阿甲' },
      { dept_name: '产品研发中心', realname: '协作测试乙', uid: 'u-2', username: '阿乙' }
    ]
  })
})

afterEach(cleanup)

describe('从通讯录加成员', () => {
  it('已经在项目里的人不再出现在候选里', async () => {
    await mountPanel()

    // 列出已在项目里的人，点了只会撞「已经是成员」——那是一次注定失败的往返。
    await waitFor(() => expect(screen.getByTestId('collab-invite-u-2')).toBeTruthy())
    expect(screen.queryByTestId('collab-invite-u-1')).toBeNull()
  })

  it('加成员失败时把网关那句话原样说出来', async () => {
    api.inviteCollabMember.mockRejectedValue(new Error('只有项目负责人能加成员'))
    await mountPanel()

    fireEvent.click(await screen.findByTestId('collab-invite-u-2'))

    // 「只有负责人能加」的判定在服务端。客户端预判会造成两份判定，而它们会漂 ——
    // 漂的方向通常是客户端先放行，然后用户收到一个莫名其妙的 403。
    expect((await screen.findByTestId('collab-invite-error')).textContent).toContain('只有项目负责人能加成员')
  })

  it('加成功之后重拉成员列表', async () => {
    api.inviteCollabMember.mockResolvedValue({ id: 9, rejoined: false })
    await mountPanel()

    fireEvent.click(await screen.findByTestId('collab-invite-u-2'))

    await waitFor(() => expect(api.inviteCollabMember).toHaveBeenCalledWith('demo', 'u-2', '阿乙'))
    await waitFor(() => expect(api.getCollabMembers).toHaveBeenCalled())
  })

  it('通讯录读不到时显示「没找到人」而不是炸掉', async () => {
    api.getCollabDirectory.mockRejectedValue(new Error('sidecar 连不上'))
    await mountPanel()

    expect(await screen.findByText('没找到人')).toBeTruthy()
  })
})
