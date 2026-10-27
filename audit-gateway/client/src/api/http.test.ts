import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearTokens,
  request,
  requestRaw,
  setTokens,
  setUnauthorizedHandler,
  uploadRaw,
} from '@/api/http'

function apiResponse (code: number, data: unknown = {}, message = ''): Response {
  return Response.json({ code, data, message }, {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('HTTP authentication recovery', () => {
  beforeEach(() => {
    clearTokens()
    vi.restoreAllMocks()
  })

  it('keeps browser API calls same-origin and replays after a refresh', async () => {
    setTokens('old-access', 'refresh-token')
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(apiResponse(10_002))
      .mockResolvedValueOnce(apiResponse(0, {
        access_token: 'new-access',
        refresh_token: 'new-refresh',
      }))
      .mockResolvedValueOnce(apiResponse(0, { ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(request('/admin/example')).resolves.toEqual({ ok: true })

    expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
      '/admin/example',
      '/admin/user/refresh',
      '/admin/example',
    ])
    expect(fetchMock.mock.calls[2]?.[1]?.headers).toMatchObject({
      Authorization: 'Bearer new-access',
    })
  })

  it('invalidates the session when the account is disabled', async () => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(apiResponse(10_007)))

    await expect(request('/admin/example')).rejects.toMatchObject({ code: 10_007 })
    expect(unauthorized).toHaveBeenCalledOnce()
  })

  it('invalidates the session when a replay still has an invalid access token', async () => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    setTokens('old-access', 'refresh-token')
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(apiResponse(10_002))
      .mockResolvedValueOnce(apiResponse(0, {
        access_token: 'new-access',
        refresh_token: 'new-refresh',
      }))
      .mockResolvedValueOnce(apiResponse(10_002)))

    await expect(request('/admin/example')).rejects.toMatchObject({ code: 10_002 })
    expect(unauthorized).toHaveBeenCalledOnce()
  })

  it('replays a multipart website upload with the same file after refreshing', async () => {
    setTokens('old-access', 'refresh-token')
    const form = new FormData()
    form.set('upload', new Blob(['<h1>ok</h1>'], { type: 'text/html' }), 'index.html')
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ detail: '请先登录' }, { status: 401 }))
      .mockResolvedValueOnce(apiResponse(0, {
        access_token: 'new-access',
        refresh_token: 'new-refresh',
      }))
      .mockResolvedValueOnce(Response.json({ id: 'site-1' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(uploadRaw('/sites', form)).resolves.toEqual({ id: 'site-1' })
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
      '/sites', '/admin/user/refresh', '/sites',
    ])
    expect(fetchMock.mock.calls[2]?.[1]?.body).toBe(form)
    expect(fetchMock.mock.calls[2]?.[1]?.headers).toMatchObject({ Authorization: 'Bearer new-access' })
  })

  it('surfaces OAuth-style error_description from raw endpoints', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(Response.json(
      { error: 'invalid_request', error_description: '回调地址有重复' }, { status: 400 },
    )))
    await expect(requestRaw('/admin/sso/clients')).rejects.toMatchObject({ message: '回调地址有重复' })
  })
})
