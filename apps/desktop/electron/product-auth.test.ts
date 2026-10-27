import { describe, expect, it, vi } from 'vitest'

import {
  classifyProductGatewayFailure,
  loginAndProvisionProductSession,
  productDingtalkEnabled,
  productDingtalkFailureMessage,
  ProductGatewayError,
  type ProductSession,
  ProductSessionCoordinator,
  provisionProductSession
} from './product-auth'

const session: ProductSession = {
  accessToken: 'jwt',
  apiKey: 'device-key',
  apiKeyId: 7,
  refreshToken: 'refresh',
  username: '员工'
}

describe('product gateway failure classification', () => {
  it('distinguishes a slow gateway from a failed connection and a rejected response', () => {
    // Regression: all three failures used to become the same "check your
    // network" banner. A 15s AbortSignal timeout means the gateway may simply
    // be slow, while an HTTP response proves the connection itself worked.
    expect([
      classifyProductGatewayFailure(new DOMException('The operation was aborted due to timeout', 'TimeoutError')),
      classifyProductGatewayFailure(Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } })),
      classifyProductGatewayFailure(new ProductGatewayError('Bad gateway', { status: 502 }))
    ]).toEqual(['timeout', 'connection', 'response'])
  })
})

describe('product account login', () => {
  it('exchanges account credentials for a per-device API key without returning the password', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ code: 0, data: { access_token: 'jwt', refresh_token: 'refresh' } })
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ code: 0, data: { id: 7, api_key: 'device-key' } })
      })

    const session = await loginAndProvisionProductSession({
      deviceName: 'MacBook',
      fetchImpl,
      gatewayOrigin: 'http://127.0.0.1:8000',
      password: 'secret',
      username: ' 员工 '
    })

    expect(session).toEqual({
      accessToken: 'jwt',
      apiKey: 'device-key',
      apiKeyId: 7,
      refreshToken: 'refresh',
      username: '员工'
    })
    expect(fetchImpl.mock.calls[1]?.[1]?.headers).toMatchObject({ Authorization: 'Bearer jwt' })
    expect(session).not.toHaveProperty('password')
  })

  it('does not provision a key after failed login', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ code: 10001, message: '用户名或密码错误' })
    })

    await expect(
      loginAndProvisionProductSession({
        deviceName: 'Windows PC',
        fetchImpl,
        gatewayOrigin: 'http://127.0.0.1:8000',
        password: 'wrong',
        username: '员工'
      })
    ).rejects.toThrow('用户名或密码错误')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
})

describe('product session lifecycle', () => {
  it('rotates an expired access token and validates both the account and device key', async () => {
    const expired = Buffer.from(JSON.stringify({ exp: 1 })).toString('base64url')

    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ code: 0, data: { access_token: 'fresh-jwt', refresh_token: 'fresh-refresh' } })
      })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ code: 0, data: { uid: 'u1' } }) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ object: 'list', data: [] }) })

    const coordinator = new ProductSessionCoordinator({
      fetchImpl,
      gatewayOrigin: 'http://127.0.0.1:7899',
      nowSeconds: () => 10
    })

    await expect(coordinator.validate({ ...session, accessToken: `x.${expired}.y` })).resolves.toMatchObject({
      accessToken: 'fresh-jwt',
      refreshToken: 'fresh-refresh'
    })
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      'http://127.0.0.1:7899/admin/user/refresh',
      'http://127.0.0.1:7899/admin/user/info',
      'http://127.0.0.1:7899/v1/models'
    ])
  })

  it('coalesces concurrent refreshes so a one-time refresh token is consumed once', async () => {
    let releaseRefresh: ((value: unknown) => void) | undefined
    let refreshed = false

    const refreshResponse = new Promise(resolve => {
      releaseRefresh = resolve
    })

    const fetchImpl = vi.fn(async (url: string | URL | Request) => {
      const href = String(url)

      if (href.endsWith('/admin/user/refresh')) {
        return refreshResponse as never
      }

      if (href.endsWith('/admin/user/info')) {
        return {
          ok: true,
          status: 200,
          json: async () => (refreshed ? { code: 0, data: { uid: 'u1' } } : { code: 10002, message: 'expired' })
        } as never
      }

      return { ok: true, status: 200, json: async () => ({ object: 'list', data: [] }) } as never
    })

    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: 'http://127.0.0.1:7899' })
    const expiredSession = { ...session, accessToken: 'not-a-jwt' }

    const first = coordinator.validate(expiredSession)
    const second = coordinator.validate(expiredSession)

    await vi.waitFor(() =>
      expect(fetchImpl.mock.calls.filter(([url]) => String(url).endsWith('/admin/user/refresh'))).toHaveLength(1)
    )
    refreshed = true
    releaseRefresh?.({
      ok: true,
      status: 200,
      json: async () => ({ code: 0, data: { access_token: 'fresh-jwt', refresh_token: 'fresh-refresh' } })
    })

    await expect(Promise.all([first, second])).resolves.toHaveLength(2)
    expect(fetchImpl.mock.calls.filter(([url]) => String(url).endsWith('/admin/user/refresh'))).toHaveLength(1)
  })

  it('does not share a refresh across different account sessions', async () => {
    const fetchImpl = vi.fn(async (url: string | URL | Request, options?: RequestInit) => {
      const href = String(url)

      if (href.endsWith('/admin/user/info')) {
        const authorization = String((options?.headers as Record<string, string>)?.Authorization || '')

        return {
          ok: true,
          status: 200,
          json: async () =>
            authorization.includes('access-account')
              ? { code: 0, data: { uid: 'u1' } }
              : { code: 10002, message: 'expired' }
        } as never
      }

      if (href.endsWith('/admin/user/refresh')) {
        const refreshToken = JSON.parse(String(options?.body)).refresh_token

        return {
          ok: true,
          status: 200,
          json: async () => ({
            code: 0,
            data: { access_token: `access-${refreshToken}`, refresh_token: `next-${refreshToken}` }
          })
        } as never
      }

      return { ok: true, status: 200, json: async () => ({ object: 'list', data: [] }) } as never
    })

    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: 'http://127.0.0.1:7899' })

    const [first, second] = await Promise.all([
      coordinator.validate({ ...session, refreshToken: 'account-a' }),
      coordinator.validate({ ...session, apiKey: 'device-b', refreshToken: 'account-b', username: '员工B' })
    ])

    expect(first.refreshToken).toBe('next-account-a')
    expect(second.refreshToken).toBe('next-account-b')
    expect(fetchImpl.mock.calls.filter(([url]) => String(url).endsWith('/admin/user/refresh'))).toHaveLength(2)
  })

  it('does not retry a disabled account forever', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ code: 10007, message: '账户被禁用' })
    })

    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: 'http://127.0.0.1:7899' })

    await expect(coordinator.validate(session)).rejects.toMatchObject({ code: 10007 })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('revokes the device key before the access and refresh tokens', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ revoked: true }) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ code: 0, data: {} }) })

    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: 'http://127.0.0.1:7899' })

    await expect(coordinator.revoke(session)).resolves.toBe(true)

    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      'http://127.0.0.1:7899/v1/device/revoke',
      'http://127.0.0.1:7899/admin/user/logout'
    ])
    expect(JSON.parse(String(fetchImpl.mock.calls[1]?.[1]?.body))).toEqual({ refresh_token: 'refresh' })
  })

  it('treats an already invalid device key as revoked and still revokes login tokens', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({ error: { message: 'API Key 已吊销' } })
      })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ code: 0, data: {} }) })

    const coordinator = new ProductSessionCoordinator({ fetchImpl, gatewayOrigin: 'http://127.0.0.1:7899' })

    await expect(coordinator.revoke(session)).resolves.toBe(true)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })
})

describe('DingTalk session and initial password', () => {
  const response = (data: unknown) => ({ ok: true, status: 200, json: async () => ({ code: 0, data }) })
  it('provisions a DingTalk gateway token through the same device key path', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response({ id: 42, api_key: 'ding-device-key' }))

    const session = await provisionProductSession(
      { deviceName: 'Local Mac', fetchImpl, gatewayOrigin: 'http://127.0.0.1:7899', username: '花名' },
      { access_token: 'ding-jwt', refresh_token: 'ding-refresh' }
    )

    expect(session.username).toBe('花名')
    expect(fetchImpl.mock.calls[0]?.[0]).toContain('/admin/apikey/create')
    expect(fetchImpl.mock.calls[0]?.[1]?.headers).toMatchObject({ Authorization: 'Bearer ding-jwt' })
  })
  it('revokes an initial-password session without provisioning a device key', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        response({ access_token: 'restricted', refresh_token: 'restricted-r', user: { must_change_password: true } })
      )
      .mockResolvedValueOnce(response({}))

    await expect(
      loginAndProvisionProductSession({
        deviceName: 'Local Mac',
        fetchImpl,
        gatewayOrigin: 'http://127.0.0.1:7899',
        username: '新员工',
        password: 'Initial-password'
      })
    ).rejects.toMatchObject({ code: 10012 })
    expect(fetchImpl.mock.calls.map(call => call[0])).toEqual([
      'http://127.0.0.1:7899/admin/user/login',
      'http://127.0.0.1:7899/admin/user/logout'
    ])
  })
  it('changes the initial password then logs in again before provisioning', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        response({ access_token: 'restricted', refresh_token: 'restricted-r', user: { must_change_password: true } })
      )
      .mockResolvedValueOnce(response({}))
      .mockResolvedValueOnce(response({}))
      .mockResolvedValueOnce(
        response({ access_token: 'fresh', refresh_token: 'fresh-r', user: { must_change_password: false } })
      )
      .mockResolvedValueOnce(response({ id: 42, api_key: 'new-key' }))

    const session = await loginAndProvisionProductSession({
      deviceName: 'Local Mac',
      fetchImpl,
      gatewayOrigin: 'http://127.0.0.1:7899',
      username: '新员工',
      password: 'Initial-password',
      newPassword: 'Personal-password'
    })

    expect(session.accessToken).toBe('fresh')
    expect(JSON.parse(String(fetchImpl.mock.calls[1]?.[1]?.body))).toEqual({
      current_password: 'Initial-password',
      new_password: 'Personal-password'
    })
    expect(JSON.parse(String(fetchImpl.mock.calls[3]?.[1]?.body))).toMatchObject({ password: 'Personal-password' })
    expect(fetchImpl.mock.calls[4]?.[1]?.headers).toMatchObject({ Authorization: 'Bearer fresh' })
  })
})

it('explains missing admin binding and preserves network errors', () => {
  expect(productDingtalkFailureMessage(new Error('Gateway rejected native login: unbound'))).toContain(
    '绑定钉钉账号'
  )
  expect(productDingtalkFailureMessage(new Error('Gateway rejected native login: unbound'))).toContain(
    '原账号密码登录'
  )
  expect(productDingtalkFailureMessage(new Error('fetch failed'))).toBe('fetch failed')
  expect(productDingtalkFailureMessage(new Error('Sign-in cancelled.'))).toContain('已取消')
})


it('keeps password login available on gateways that only support web DingTalk login', async () => {
  for (const [data, expected] of [
    [{ enabled: true }, false],
    [{ enabled: true, auth_flows: ['native_pkce'] }, true],
    [{ enabled: false, auth_flows: ['native_pkce'] }, false]
  ] as const) {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({code:0,data}) }))
    expect(await productDingtalkEnabled('http://127.0.0.1:7899', fetchImpl as unknown as typeof fetch)).toBe(expected)
  }
})
