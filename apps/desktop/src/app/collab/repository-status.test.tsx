import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

const api = vi.hoisted(() => ({
  changeCollabProjectStatus: vi.fn(),
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
  removeCollabMember: vi.fn(),
  resetCollabSession: vi.fn(),
  retryCollabRepository: vi.fn(),
  sendCollabMessage: vi.fn(),
  setCollabRespondTo: vi.fn(),
  syncCollabMessages: vi.fn(),
  tickCollabAgent: vi.fn(),
  transferCollabProject: vi.fn(),
  updateCollabProject: vi.fn()
}))

vi.mock('@/hermes', () => api)

beforeEach(async () => {
  for (const fn of Object.values(api)) {
    fn.mockReset()
  }

  api.retryCollabRepository.mockResolvedValue({ git_repo_url: '', status: 'pending' })
  api.getCollabProjects.mockResolvedValue({ authenticated: true, projects: [], stale: false })

  const store = await import('@/store/collab')
  store.$collabState.setKey('demo', {
    members: [{ id: 1, is_me: true, member_kind: 'user', role: 'owner', user_uid: 'u1' }]
  } as never)
})

afterEach(cleanup)

describe('项目仓库状态', () => {
  it('失败状态可见，并允许负责人显式重试', async () => {
    const { ProjectManagement } = await import('./management')
    render(
      <I18nProvider configClient={null} initialLocale="zh">
        <ProjectManagement
          project={{
            code: 'demo',
            git_repo_error: '连接 Gitea 超时',
            git_repo_status: 'failed',
            my_role: 'owner'
          }}
        />
      </I18nProvider>
    )

    fireEvent.click(screen.getByText('项目设置'))
    expect(screen.getByRole('alert').textContent).toContain('连接 Gitea 超时')
    fireEvent.click(screen.getByText('重试创建'))

    await waitFor(() => expect(api.retryCollabRepository).toHaveBeenCalledWith('demo'))
  })
})
