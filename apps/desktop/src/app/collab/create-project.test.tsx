import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { zh } from '@/i18n/zh'

/**
 * 在客户端建项目（2026-09-07）。
 *
 * 建项目原本只在网关后台的「协作」页 —— 而后台的定位是看板。这一组守两件事：
 *
 * 1. **写失败绝不能伪装成建好了**。表单里报出网关那句原话，而不是关掉表单让人
 *    去列表里找一个根本不存在的项目
 * 2. **不在客户端抄一份 code 规则**。规则在服务端，抄一份迟早会漂 ——
 *    漂的那天用户看到的是「客户端说可以，服务端说不行」
 */

const api = vi.hoisted(() => ({
  claimCollabTask: vi.fn(),
  confirmCollabTask: vi.fn(),
  createCollabProject: vi.fn(),
  createCollabTask: vi.fn(),
  getCollabDirectory: vi.fn(),
  inviteCollabMember: vi.fn(),
  getCollabMembers: vi.fn(),
  getCollabMessages: vi.fn(),
  getCollabProjects: vi.fn(),
  getCollabTasks: vi.fn(),
  getCollabTurns: vi.fn(),
  pullMyCollabAgent: vi.fn(),
  resetCollabSession: vi.fn(),
  sendCollabMessage: vi.fn(),
  setCollabRespondTo: vi.fn(),
  syncCollabMessages: vi.fn(),
  tickCollabAgent: vi.fn()
}))

vi.mock('@/hermes', () => api)
vi.mock('./task-result', () => ({ TaskResult: () => null }))
vi.mock('./task-execution', () => ({ useTaskExecution: () => async () => {}, taskBinding: () => undefined }))

async function mount() {
  const { CollabView } = await import('./index')

  render(
    <I18nProvider configClient={null} initialLocale="zh">
      <CollabView />
    </I18nProvider>
  )

  await waitFor(() => expect(api.getCollabProjects).toHaveBeenCalled())
  fireEvent.click(screen.getByTestId('collab-new-project'))
}

beforeEach(async () => {
  for (const fn of Object.values(api)) {
    fn.mockReset()
  }

  // store 是模块级的，用例之间会串。尤其 $collabProjectsLoaded ——
  // 上一条用例成功读过一次，下一条就再也验不到「没读到」那个状态。
  const store = await import('@/store/collab')

  store.$collabProjects.set([])
  store.$collabProjectsLoaded.set(false)
  store.$collabProjectsStale.set(false)
  store.$collabAuthenticated.set(true)
  store.$collabSelected.set('')

  api.getCollabProjects.mockResolvedValue({ authenticated: true, projects: [], stale: false })
  api.getCollabMembers.mockResolvedValue({ members: [], stale: false })
  api.getCollabTasks.mockResolvedValue({ stale: false, tasks: [] })
  api.getCollabMessages.mockResolvedValue({ messages: [], stale: false })
  api.getCollabTurns.mockResolvedValue({ hop_limit: 8, hops: 0, stale: false, turns: [] })
  api.getCollabDirectory.mockResolvedValue({ authenticated: true, stale: false, users: [] })
})

afterEach(cleanup)

describe('在客户端建项目', () => {
  it('空的项目标识按不下去 —— 不拿一个必然失败的请求去打网关', async () => {
    await mount()

    expect((screen.getByTestId('collab-new-submit') as HTMLButtonElement).disabled).toBe(true)
    expect(api.createCollabProject).not.toHaveBeenCalled()
  })

  it('建好之后列表会重拉，新项目才可能出现', async () => {
    api.createCollabProject.mockResolvedValue({ code: 'q4', id: 3 })
    await mount()

    fireEvent.change(screen.getByTestId('collab-new-code'), { target: { value: 'q4' } })
    fireEvent.change(screen.getByTestId('collab-new-name'), { target: { value: '四季度' } })
    fireEvent.click(screen.getByTestId('collab-new-submit'))

    await waitFor(() => expect(api.createCollabProject).toHaveBeenCalledWith('q4', '四季度', ''))
    // 建完不重拉的话，界面上是「点了没反应」——而项目其实建出来了。
    await waitFor(() => expect(api.getCollabProjects.mock.calls.length).toBeGreaterThan(1))
  })

  it('网关拒绝时原样显示那句话，表单不关', async () => {
    api.createCollabProject.mockRejectedValue(new Error('项目标识 q4 已被占用'))
    await mount()

    fireEvent.change(screen.getByTestId('collab-new-code'), { target: { value: 'q4' } })
    fireEvent.click(screen.getByTestId('collab-new-submit'))

    // 「已被占用」是**服务端**判出来的。客户端抄一份 code 规则就会有两份判定，
    // 而这句原话恰恰是用户唯一能据以改对的东西。
    expect((await screen.findByTestId('collab-new-error')).textContent).toContain('项目标识 q4 已被占用')
    expect(screen.getByTestId('collab-new-code')).toBeTruthy()
  })

  it('文案不再把人指回后台 —— 后台已经是只读看板了', () => {
    expect(zh.collab.emptyHint).not.toContain('后台')
    expect(zh.collab.create.submit).toBeTruthy()
  })

  it('侧栏那一行有文案 —— 空 label 会渲染成一行看不见的按钮', () => {
    // 2026-09-07 真机上就是这么表现的：图标在、文字没有。`sidebar.nav` 当时是
    // `Record<string, string>`，少一个键不报错。类型已经收紧，这条是双保险。
    expect(zh.sidebar.nav.collab).toBeTruthy()
  })
})

describe('列表读不到时不能冒充成「你没有项目」', () => {
  it('第一次读失败：不说那句话，改说取不到并继续重试', async () => {
    vi.useFakeTimers()

    try {
      api.getCollabProjects.mockRejectedValueOnce(new Error('sidecar 还没起来'))
      api.getCollabProjects.mockResolvedValue({
        authenticated: true,
        projects: [{ code: 'q4', my_role: 'owner', name: '四季度' }],
        stale: false
      })

      const { CollabView } = await import('./index')

      render(
        <I18nProvider configClient={null} initialLocale="zh">
          <CollabView />
        </I18nProvider>
      )

      await vi.waitFor(() => expect(api.getCollabProjects).toHaveBeenCalledTimes(1))

      // 这句是对一个我们**没拿到**的数据下的断言。读失败时它比不显示更糟。
      expect(screen.queryByText(zh.collab.empty)).toBeNull()
      expect(screen.getByTestId('collab-projects-unavailable')).toBeTruthy()

      // 只读一次的话，这一页会在整个会话里停在「取不到」。
      await vi.advanceTimersByTimeAsync(1_500)
      await vi.waitFor(() => expect(api.getCollabProjects).toHaveBeenCalledTimes(2))
      await vi.waitFor(() => expect(screen.getByText('四季度')).toBeTruthy())
      expect(screen.queryByTestId('collab-projects-unavailable')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('真的一个项目都没有时，那句话照说', async () => {
    api.getCollabProjects.mockResolvedValue({ authenticated: true, projects: [], stale: false })

    const { CollabView } = await import('./index')

    render(
      <I18nProvider configClient={null} initialLocale="zh">
        <CollabView />
      </I18nProvider>
    )

    expect(await screen.findByText(zh.collab.empty)).toBeTruthy()
    expect(screen.queryByTestId('collab-projects-unavailable')).toBeNull()
  })
})
