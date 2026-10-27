export interface ProductSession {
  accessToken: string
  apiKey: string
  apiKeyId: number
  refreshToken: string
  username: string
}

interface ProductLoginOptions {
  deviceName: string
  fetchImpl?: typeof fetch
  gatewayOrigin: string
  password: string
  newPassword?: string
  username: string
}

interface GatewayEnvelope<T> {
  code?: number
  data?: T
  message?: string
}

interface ProductSessionCoordinatorOptions {
  fetchImpl?: typeof fetch
  gatewayOrigin: string
  nowSeconds?: () => number
}

const ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60
const AUTH_ERROR_CODES = new Set([10002, 10003, 10007])

const CONNECTION_ERROR_CODES = new Set([
  'EAI_AGAIN',
  'ECONNREFUSED',
  'ECONNRESET',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'ENOTFOUND',
  'UND_ERR_CONNECT_TIMEOUT'
])

export type ProductGatewayFailureKind = 'connection' | 'response' | 'timeout'

export class ProductGatewayError extends Error {
  readonly code: number | null
  readonly status: number

  constructor(message: string, options: { code?: number | null; status?: number } = {}) {
    super(message)
    this.name = 'ProductGatewayError'
    this.code = Number.isInteger(options.code) ? Number(options.code) : null
    this.status = options.status ?? 0
  }
}

/**
 * 这次鉴权失败是**管理员干的**，还是**会话自己过期了**？
 *
 * 两者要给用户完全相反的指示，混成一句的代价在 2026-09-01 兑现了：休眠唤醒的
 * 用户被弹回登录页，并被告知「你已被管理员强制下线」—— 管理员什么都没做，
 * 真正的原因是网关那边 Redis 抖了一下把刷新请求判成了已撤销。
 *
 * 用户拿着这句话去找管理员，管理员一看账号好好的，两边都白跑一趟。
 *
 * - 10007 / 403 = 账号被禁用或没权限 —— **确实是人为动作**，说找管理员是对的
 * - 10002 / 10003 / 401 = 令牌过期或失效 —— 重新登录就好，别惊动任何人
 */
export function isProductAccountDisabledError(error: unknown): boolean {
  return error instanceof ProductGatewayError && (error.code === 10007 || error.status === 403)
}

export function isProductGatewayAuthError(error: unknown): boolean {
  return (
    error instanceof ProductGatewayError &&
    (AUTH_ERROR_CODES.has(error.code ?? 0) || error.status === 401 || error.status === 403)
  )
}

function errorField(error: unknown, key: 'cause' | 'code' | 'message' | 'name'): unknown {
  return (typeof error === 'object' || typeof error === 'function') && error !== null
    ? Reflect.get(error, key)
    : undefined
}

/**
 * Turn a failed product-gateway probe into the user action it justifies.
 *
 * A completed HTTP response proves the network path worked, even when the
 * gateway returned 429/5xx or malformed JSON. Only known transport failures
 * should tell the user to inspect their network; the 15s AbortSignal deadline
 * is reported separately because the gateway may simply be busy.
 */
export function classifyProductGatewayFailure(error: unknown): ProductGatewayFailureKind {
  const chain: unknown[] = []
  const seen = new Set<unknown>()
  let current = error

  while ((typeof current === 'object' || typeof current === 'function') && current !== null && !seen.has(current)) {
    seen.add(current)
    chain.push(current)
    current = errorField(current, 'cause')
  }

  if (chain.some(item => item instanceof ProductGatewayError)) {
    return 'response'
  }

  for (const item of chain) {
    const name = String(errorField(item, 'name') ?? '')
    const message = String(errorField(item, 'message') ?? '')
    const code = String(errorField(item, 'code') ?? '')

    if (name === 'TimeoutError' || code === 'ETIMEDOUT' || /timed?\s*out|timeout/i.test(message)) {
      return 'timeout'
    }
  }

  for (const item of chain) {
    const message = String(errorField(item, 'message') ?? '')
    const code = String(errorField(item, 'code') ?? '')

    if (CONNECTION_ERROR_CODES.has(code) || /fetch failed|network error|failed to fetch/i.test(message)) {
      return 'connection'
    }
  }

  return 'response'
}

async function gatewayRequest<T>(url: string, options: RequestInit, fetchImpl: typeof fetch): Promise<T> {
  const response = await fetchImpl(url, {
    ...options,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...options.headers },
    signal: AbortSignal.timeout(15_000)
  })

  let envelope: GatewayEnvelope<T>

  try {
    envelope = (await response.json()) as GatewayEnvelope<T>
  } catch {
    throw new ProductGatewayError(`Company gateway returned invalid JSON (HTTP ${response.status}).`, {
      status: response.status
    })
  }

  if (!response.ok || envelope.code !== 0 || envelope.data === undefined) {
    throw new ProductGatewayError(envelope.message || `Company gateway request failed (HTTP ${response.status}).`, {
      code: envelope.code,
      status: response.status
    })
  }

  return envelope.data
}

async function gatewayCommand(url: string, options: RequestInit, fetchImpl: typeof fetch): Promise<void> {
  await gatewayRequest<unknown>(url, options, fetchImpl)
}

async function verifyDeviceApiKey(gatewayOrigin: string, apiKey: string, fetchImpl: typeof fetch): Promise<void> {
  const response = await fetchImpl(`${gatewayOrigin}/v1/models`, {
    headers: { Accept: 'application/json', Authorization: `Bearer ${apiKey}` },
    signal: AbortSignal.timeout(15_000)
  })

  let body: any

  try {
    body = await response.json()
  } catch {
    throw new ProductGatewayError(`Company gateway returned invalid JSON (HTTP ${response.status}).`, {
      status: response.status
    })
  }

  if (response.ok && body?.object === 'list' && Array.isArray(body.data)) {
    return
  }

  throw new ProductGatewayError(
    body?.error?.message ||
      body?.message ||
      `Company gateway rejected the device credential (HTTP ${response.status}).`,
    { code: body?.code, status: response.status }
  )
}

function jwtExpiresAt(token: string): number | null {
  try {
    const parts = token.split('.')

    if (parts.length !== 3) {
      return null
    }

    const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'))
    const expiresAt = Number(payload?.exp)

    return Number.isFinite(expiresAt) ? expiresAt : null
  } catch {
    return null
  }
}

export function productAccessTokenNeedsRefresh(
  token: string,
  nowSeconds: number,
  skewSeconds = ACCESS_TOKEN_REFRESH_SKEW_SECONDS
): boolean {
  const expiresAt = jwtExpiresAt(token)

  return expiresAt !== null && nowSeconds >= expiresAt - skewSeconds
}

async function revokeIssuedTokens(
  gatewayOrigin: string,
  tokens: { access_token: string; refresh_token: string },
  fetchImpl: typeof fetch
): Promise<void> {
  await gatewayCommand(
    `${gatewayOrigin}/admin/user/logout`,
    {
      method: 'POST',
      body: JSON.stringify({ refresh_token: tokens.refresh_token }),
      headers: { Authorization: `Bearer ${tokens.access_token}` }
    },
    fetchImpl
  )
}

export async function loginAndProvisionProductSession({
  deviceName,
  fetchImpl = fetch,
  gatewayOrigin,
  password,
  newPassword,
  username
}: ProductLoginOptions): Promise<ProductSession> {
  const normalizedUsername = username.trim()

  if (!normalizedUsername || !password) {
    throw new Error('Username and password are required.')
  }

  const tokens = await gatewayRequest<{
    access_token: string
    refresh_token: string
    user?: { must_change_password?: boolean }
  }>(
    `${gatewayOrigin}/admin/user/login`,
    { method: 'POST', body: JSON.stringify({ username: normalizedUsername, password }) },
    fetchImpl
  )

  if (!tokens.access_token || !tokens.refresh_token) {
    throw new Error('Company gateway login response did not include tokens.')
  }

  if (tokens.user?.must_change_password) {
    try {
      if (!newPassword) {
        throw new ProductGatewayError('请先修改初始密码', { code: 10012 })
      }

      await gatewayRequest(
        `${gatewayOrigin}/admin/account/me/password`,
        {
          method: 'POST',
          headers: { Authorization: `Bearer ${tokens.access_token}` },
          body: JSON.stringify({ current_password: password, new_password: newPassword })
        },
        fetchImpl
      )
    } finally {
      await revokeIssuedTokens(gatewayOrigin, tokens, fetchImpl).catch(() => undefined)
    }

    return loginAndProvisionProductSession({ deviceName, fetchImpl, gatewayOrigin, username, password: newPassword })
  }

  return provisionProductSession({ deviceName, fetchImpl, gatewayOrigin, username: normalizedUsername }, tokens)
}

export async function productDingtalkEnabled(gatewayOrigin: string, fetchImpl = fetch): Promise<boolean> {
  const config = await gatewayRequest<{ enabled: boolean; auth_flows?: string[] }>(
    `${gatewayOrigin}/admin/user/dingtalk/config`,
    {},
    fetchImpl
  )

  return config.enabled === true && Array.isArray(config.auth_flows) && config.auth_flows.includes('native_pkce')
}

export async function provisionProductSession(
  { deviceName, fetchImpl = fetch, gatewayOrigin, username }: Omit<ProductLoginOptions, 'password' | 'newPassword'>,
  tokens: { access_token: string; refresh_token: string }
): Promise<ProductSession> {
  if (!tokens.access_token || !tokens.refresh_token || !username) {
    throw new Error('Company gateway login response did not include a usable identity and tokens.')
  }

  let created: { api_key: string; id: number }

  try {
    created = await gatewayRequest<{ api_key: string; id: number }>(
      `${gatewayOrigin}/admin/apikey/create`,
      {
        method: 'POST',
        body: JSON.stringify({ name: deviceName.slice(0, 64) || 'Desktop client' }),
        headers: { Authorization: `Bearer ${tokens.access_token}` }
      },
      fetchImpl
    )
  } catch (error) {
    // Login already minted a live access/refresh pair. If device provisioning
    // fails, revoke that pair rather than leaking an unreachable session.
    await revokeIssuedTokens(gatewayOrigin, tokens, fetchImpl).catch(() => undefined)
    throw error
  }

  if (!created.api_key || !Number.isInteger(created.id)) {
    await revokeIssuedTokens(gatewayOrigin, tokens, fetchImpl).catch(() => undefined)
    throw new Error('Company gateway did not return a usable device API key.')
  }

  return {
    accessToken: tokens.access_token,
    apiKey: created.api_key,
    apiKeyId: created.id,
    refreshToken: tokens.refresh_token,
    username
  }
}

/**
 * Owns the refresh/retry policy for the one product account attached to this
 * Electron process. Concurrent callers share one refresh because refresh
 * tokens are one-time credentials and must never be replayed.
 */
export class ProductSessionCoordinator {
  private readonly fetchImpl: typeof fetch
  private readonly gatewayOrigin: string
  private readonly nowSeconds: () => number
  private readonly refreshInFlight = new Map<string, Promise<ProductSession>>()

  constructor({
    fetchImpl = fetch,
    gatewayOrigin,
    nowSeconds = () => Date.now() / 1000
  }: ProductSessionCoordinatorOptions) {
    this.fetchImpl = fetchImpl
    this.gatewayOrigin = gatewayOrigin
    this.nowSeconds = nowSeconds
  }

  private refresh(session: ProductSession): Promise<ProductSession> {
    const existing = this.refreshInFlight.get(session.refreshToken)

    if (existing) {
      return existing
    }

    const pending = gatewayRequest<{ access_token: string; refresh_token: string }>(
      `${this.gatewayOrigin}/admin/user/refresh`,
      { method: 'POST', body: JSON.stringify({ refresh_token: session.refreshToken }) },
      this.fetchImpl
    ).then(tokens => {
      if (!tokens.access_token || !tokens.refresh_token) {
        throw new ProductGatewayError('Company gateway refresh response did not include tokens.', { code: 10003 })
      }

      return {
        ...session,
        accessToken: tokens.access_token,
        refreshToken: tokens.refresh_token
      }
    })

    const wrapped = pending.finally(() => {
      if (this.refreshInFlight.get(session.refreshToken) === wrapped) {
        this.refreshInFlight.delete(session.refreshToken)
      }
    })

    this.refreshInFlight.set(session.refreshToken, wrapped)

    return wrapped
  }

  private async validateAccessToken(
    session: ProductSession,
    onRotated?: (rotated: ProductSession) => void
  ): Promise<ProductSession> {
    let current = session

    if (productAccessTokenNeedsRefresh(current.accessToken, this.nowSeconds())) {
      current = await this.refresh(current)
      onRotated?.(current)
    }

    try {
      await gatewayRequest<Record<string, unknown>>(
        `${this.gatewayOrigin}/admin/user/info`,
        { headers: { Authorization: `Bearer ${current.accessToken}` } },
        this.fetchImpl
      )
    } catch (error) {
      if (!isProductGatewayAuthError(error) || error.code === 10007) {
        throw error
      }

      current = await this.refresh(current)
      onRotated?.(current)
      await gatewayRequest<Record<string, unknown>>(
        `${this.gatewayOrigin}/admin/user/info`,
        { headers: { Authorization: `Bearer ${current.accessToken}` } },
        this.fetchImpl
      )
    }

    return current
  }

  /**
   * `onRotated` fires the moment a refresh succeeds, before the follow-up
   * checks. Refresh tokens are one-time: the gateway revokes the old one as
   * soon as it accepts it. If the new pair were only handed back after
   * `/admin/user/info` and `/v1/models` also pass, a timeout on either of
   * those (common right after waking from sleep) would drop the only valid
   * refresh token on the floor; the next poll would then present the revoked
   * one, get 10003 and sign the user out for no reason.
   */
  async validate(
    session: ProductSession,
    options: { onRotated?: (rotated: ProductSession) => void } = {}
  ): Promise<ProductSession> {
    const current = await this.validateAccessToken(session, options.onRotated)

    await verifyDeviceApiKey(this.gatewayOrigin, current.apiKey, this.fetchImpl)

    return current
  }

  async revoke(session: ProductSession): Promise<boolean> {
    // The device key revokes itself, so logout still works when the access
    // token has expired. An auth rejection here means that key is already
    // unusable (revoked/expired/disabled), which is the desired end state.
    try {
      const response = await this.fetchImpl(`${this.gatewayOrigin}/v1/device/revoke`, {
        method: 'POST',
        headers: { Accept: 'application/json', Authorization: `Bearer ${session.apiKey}` },
        signal: AbortSignal.timeout(15_000)
      })

      const body = await response.json()

      if (!response.ok || body?.revoked !== true) {
        const error = new ProductGatewayError(
          body?.error?.message || body?.message || `Device credential revocation failed (HTTP ${response.status}).`,
          { code: body?.code, status: response.status }
        )

        if (!isProductGatewayAuthError(error)) {
          throw error
        }
      }
    } catch (error) {
      if (!isProductGatewayAuthError(error)) {
        throw error
      }
    }

    try {
      await revokeIssuedTokens(
        this.gatewayOrigin,
        { access_token: session.accessToken, refresh_token: session.refreshToken },
        this.fetchImpl
      )

      return true
    } catch (error) {
      if (isProductGatewayAuthError(error)) {
        // The device key is already gone. Rejected/expired JWTs are no longer
        // usable credentials, so the remote logout target state is satisfied.
        return true
      }

      throw error
    }
  }
}

export function parseStoredProductSession(value: unknown): ProductSession | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const session = value as Partial<ProductSession>

  if (
    !session.accessToken ||
    !session.apiKey ||
    !Number.isInteger(session.apiKeyId) ||
    !session.refreshToken ||
    !session.username
  ) {
    return null
  }

  return session as ProductSession
}

/** 已知的网关拒绝原因转成员工可操作的提示；网络错误继续保留其原因。 */
export function productDingtalkFailureMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  const match = message.match(/Gateway rejected native login: ([a-z]+)/)

  const reasons: Record<string, string> = {
    unbound: '此钉钉账号尚未关联工作台账号。请在网关登录页使用原账号密码登录，然后在右上角账号菜单中选择“绑定钉钉账号”。',
    disabled: '工作台账号不可用，请联系管理员。',
    provider: '钉钉身份校验未完成，请重试或联系管理员。',
    state: '钉钉授权状态已过期，请重新登录。',
    conflict: '钉钉身份绑定发生冲突，请联系管理员核对。'
  }

  if (match && reasons[match[1]]) {
    return reasons[match[1]]
  }

  if (/cancelled/i.test(message)) {
    return '钉钉登录已取消'
  }

  if (/Native sign-in timed out/.test(message)) {
    return '钉钉登录已超时，请重新发起登录。'
  }

  return message
}
