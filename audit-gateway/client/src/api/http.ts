/**
 * HTTP 客户端
 *
 * 网关的响应始终是 200 + {code, message, data}，业务失败靠 code 区分，
 * 所以这里把「HTTP 层错误」和「业务 code 非 0」统一收敛成 ApiError 抛出，
 * 调用方只需要 try/catch，不用每处判断 code。
 *
 * 认证走两条通道：
 * - httpOnly Cookie（后端登录时下发，刷新页面后仍在，用于恢复会话）
 * - Authorization: Bearer（内存里的 access_token，避免把令牌写进 localStorage 被 XSS 摸走）
 *
 * 遇到 10002（access_token 失效）会自动刷新一次并重放请求，
 * 多个并发请求共用同一次刷新（单飞），刷新失败则触发登出回调。
 */

import { API_CODE, type ApiEnvelope } from '@/types/api'

export class ApiError extends Error {
  readonly code: number
  readonly data: unknown

  constructor (code: number, message: string, data: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.data = data
  }

  /** 是否是「登录态问题」，调用方据此决定要不要跳登录页 */
  get isAuthError (): boolean {
    const authCodes: number[] = [
      API_CODE.ACCESS_TOKEN_INVALID,
      API_CODE.REFRESH_TOKEN_INVALID,
      API_CODE.ACCOUNT_DISABLED,
    ]
    return authCodes.includes(this.code)
  }
}

// ---------------- 令牌与回调（放模块级，避免与 store 循环依赖）----------------

let accessToken = ''
let refreshToken = ''
let onUnauthorized: (() => void) | null = null

export function setTokens (access: string, refresh: string): void {
  accessToken = access
  refreshToken = refresh
}

export function clearTokens (): void {
  accessToken = ''
  refreshToken = ''
}

export function hasRefreshToken (): boolean {
  return Boolean(refreshToken)
}

/** 由 auth store 注册：刷新失败或登录态彻底失效时调用 */
export function setUnauthorizedHandler (handler: () => void): void {
  onUnauthorized = handler
}

// ---------------- 刷新（单飞）----------------

let refreshing: Promise<boolean> | null = null

async function doRefresh (): Promise<boolean> {
  const res = await fetch('/admin/user/refresh', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    // 内存里没有 refresh_token 时传空串，由后端回退读 httpOnly Cookie
    body: JSON.stringify({ refresh_token: refreshToken }),
  })

  if (!res.ok) {
    return false
  }

  const envelope = (await res.json()) as ApiEnvelope<{ access_token: string, refresh_token: string }>
  if (envelope.code !== API_CODE.OK) {
    return false
  }

  setTokens(envelope.data.access_token, envelope.data.refresh_token)
  return true
}

function refreshOnce (): Promise<boolean> {
  refreshing ??= doRefresh()
    .catch(() => false)
    .finally(() => {
      refreshing = null
    })
  return refreshing
}

// ---------------- 请求 ----------------

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
  /** 内部用：标记这是刷新后的重放，避免无限递归 */
  retried?: boolean
}

function buildUrl (path: string, query?: RequestOptions['query']): string {
  const url = path
  if (!query) {
    return url
  }

  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') {
      params.append(key, String(value))
    }
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

export async function request<T> (path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, retried = false } = options

  const headers: Record<string, string> = {}
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }
  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`
  }

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      credentials: 'include',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    // 网络不通 / 网关没起 —— 给出比 "Failed to fetch" 更有用的提示
    throw new ApiError(API_CODE.UNKNOWN, '无法连接网关服务，请确认后端已启动')
  }

  // 网关对 404/405/500 也返回统一结构，但反向代理可能返回非 JSON
  let envelope: ApiEnvelope<T>
  try {
    envelope = (await res.json()) as ApiEnvelope<T>
  } catch {
    throw new ApiError(API_CODE.UNKNOWN, `服务异常（HTTP ${res.status}）`)
  }

  if (envelope.code === API_CODE.OK) {
    return envelope.data
  }

  // access_token 过期：刷新一次后重放。重放仍失败也必须终止会话。
  if (envelope.code === API_CODE.ACCESS_TOKEN_INVALID) {
    if (!retried) {
      const ok = await refreshOnce()
      if (ok) {
        return request<T>(path, { ...options, retried: true })
      }
    }

    clearTokens()
    onUnauthorized?.()
  }

  if (
    envelope.code === API_CODE.REFRESH_TOKEN_INVALID
    || envelope.code === API_CODE.ACCOUNT_DISABLED
  ) {
    clearTokens()
    onUnauthorized?.()
  }

  throw new ApiError(envelope.code, envelope.message, envelope.data)
}

export const http = {
  get: <T>(path: string, query?: RequestOptions['query']) => request<T>(path, { query }),
  post: <T>(path: string, body?: unknown, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'POST', body, query }),
  put: <T>(path: string, body?: unknown, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'PUT', body, query }),
}

/**
 * 调 `/v1/*` 用这个，不是上面那个 `http`。
 *
 * ## 为什么必须分成两条
 *
 * `/admin/*` 返回的是全站统一信封 `{code, message, data}`，`request()` 靠
 * `code === 0` 判成功、返回 `data`。而 `/v1/*` 是给**机器**用的接口，返回**裸
 * JSON**、拒绝走真 4xx（见 server/router/v1/skill_hub.py 的模块说明：客户端按
 * `2xx == 成功` 分支，拒绝回 200 会被当成成功）。
 *
 * 拿 `request()` 去调 `/v1/*`，`envelope.code` 是 undefined，`undefined !== 0`
 * —— **成功的响应也会抛错**。这是真的踩到了才发现的，因为它不是编译期错误。
 *
 * ## 但令牌刷新必须共用
 *
 * 只是「少拆一层信封」的话，最省事的写法是自己 fetch 一次。那样协作页会在
 * access_token 过期时直接掉线，而控制台其余页面照常静默刷新 —— 用户看到的是
 * 「就这一个页面老是要重新登录」，而没有任何一处会报错。
 *
 * 所以这里复用 `request()` 的整条认证/刷新路径，只在最后一步分叉。
 */
export async function requestRaw<T> (path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, retried = false } = options

  const headers: Record<string, string> = {}
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }
  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`
  }

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      credentials: 'include',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(API_CODE.UNKNOWN, '无法连接网关服务，请确认后端已启动')
  }

  let payload: unknown
  try {
    payload = await res.json()
  } catch {
    payload = null
  }

  if (res.ok) {
    return payload as T
  }

  // 401 走与信封那条同一套刷新重放：不这么做的话，协作页会变成
  // 「唯一一个 token 一过期就掉线的页面」。
  if (res.status === 401) {
    if (!retried) {
      const refreshed = await refreshOnce()
      if (refreshed) {
        return requestRaw<T>(path, { ...options, retried: true })
      }
    }
    clearTokens()
    onUnauthorized?.()
  }

  // AppError 的信封是 {code, message}，捞出来给人看 ——
  // 「slug 只能用小写字母…」比「HTTP 400」有用得多。
  const envelope = (payload ?? {}) as { code?: number }
  throw new ApiError(envelope.code ?? API_CODE.UNKNOWN, errorText(payload) || `请求失败（HTTP ${res.status}）`)
}

/** 网关有三种错误体：AppError 信封、FastAPI 的 detail、OAuth 风格的 error_description。 */
function errorText (payload: unknown): string {
  const body = (payload ?? {}) as { message?: unknown, detail?: unknown, error_description?: unknown }
  for (const value of [body.message, body.detail, body.error_description]) {
    if (typeof value === 'string' && value) {
      return value
    }
  }
  return ''
}

/** multipart 上传沿用相同的会话刷新；浏览器负责 boundary，不能手写 Content-Type。 */
export async function uploadRaw<T> (path: string, form: FormData, retried = false): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method: 'POST',
      credentials: 'include',
      headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
      body: form,
    })
  } catch {
    throw new ApiError(API_CODE.UNKNOWN, '无法连接网关服务，请确认后端已启动')
  }
  let payload: unknown
  try {
    payload = await res.json()
  } catch {
    payload = null
  }
  if (res.ok) {
    return payload as T
  }
  if (res.status === 401) {
    if (!retried && await refreshOnce()) {
      return uploadRaw<T>(path, form, true)
    }
    clearTokens()
    onUnauthorized?.()
  }
  const error = (payload ?? {}) as { code?: number }
  throw new ApiError(error.code ?? API_CODE.UNKNOWN, errorText(payload) || `上传失败（HTTP ${res.status}）`)
}

/** `/v1/*` 的便捷封装。与上面的 `http` 是两条路，别混用 —— 见 requestRaw 的说明 */
export const httpV1 = {
  get: <T>(path: string, query?: RequestOptions['query']) => requestRaw<T>(path, { query }),
  post: <T>(path: string, body?: unknown) => requestRaw<T>(path, { method: 'POST', body }),
  del: <T>(path: string, query?: RequestOptions['query']) =>
    requestRaw<T>(path, { method: 'DELETE', query }),
}
