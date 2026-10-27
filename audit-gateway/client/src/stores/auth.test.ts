import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as authApi from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  fetchCurrentUser: vi.fn(),
  logout: vi.fn(),
}))

const userContext = {
  id: 1,
  uid: 'president-1',
  username: '总裁',
  nickname: null,
  realname: '测试总裁',
  status: 1,
  permission: 'president',
  dept_id: 7,
  dept_name: '管理部门',
  dept_code: 'exec',
  expired: null,
  permissions: ['user:view', 'audit:view'],
  default_route: 'dashboard',
}

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.resetAllMocks()
  })

  it('uses the user context returned by login without a second bootstrap request', async () => {
    vi.mocked(authApi.login).mockResolvedValue({
      token: 'access',
      access_token: 'access',
      refresh_token: 'refresh',
      token_type: 'bearer',
      user: userContext,
    })
    vi.mocked(authApi.fetchCurrentUser).mockRejectedValue(
      new Error('the second request must not be required'),
    )

    const auth = useAuthStore()
    await auth.login('总裁', 'secret')

    expect(auth.user).toEqual(userContext)
    expect(auth.initialized).toBe(true)
    expect(authApi.fetchCurrentUser).not.toHaveBeenCalled()
  })
})
