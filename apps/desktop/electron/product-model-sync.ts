/**
 * 登录后替用户选定初始模型（阶段 4 · CP3 发现的产品缺陷）。
 *
 * ## 为什么需要它
 *
 * 托管配置只钉死 provider 和 base_url，**没有 model.default**。于是全新用户
 * 登录成功 → 打字 → 网关返回 `HTTP 400: 缺少 model 参数`。使用者是业务人员，
 * 遇到这种报错只会截图来问，而真实原因只是"还没挑模型"。
 *
 * ## 为什么走 HTTP 而不是直接写文件
 *
 * Electron 不碰 Hermes 的配置文件。`POST /api/model/set` 是 Hermes 自己提供的
 * 接口，由**它自己**写 `~/.hermes/config.yaml`，并且已经被品牌策略守着
 * （`model_switch_allowed` 不通过就 403）。桌面端的模型选择器本来就走这条路，
 * 自动选型只是替用户调了一次同样的接口。
 *
 * ## 三种情况
 *
 * 见 `decideModelSelection`。其中"清单为空"刻意**不静默**：能进聊天界面却发不出
 * 消息，对业务人员来说就是"这软件坏了"，而事实是管理员还没给他开通模型。
 */

export interface ModelSelectionDecision {
  /** 要写入的模型名；null 表示不需要写 */
  model: string | null
  reason: 'selected' | 'kept' | 'no-grants' | 'regranted'
}

/**
 * 按字母序取第一个。
 *
 * 排序而不是取网关返回的原始顺序：上游清单顺序没有稳定性保证，
 * 换个顺序就换个默认模型，用户会觉得"它自己变了"。
 */
export function firstGrantedModel(models: readonly string[]): string | null {
  const sorted = [...models]
    .map(name => String(name || '').trim())
    .filter(Boolean)
    .sort((left, right) => left.localeCompare(right))

  return sorted[0] ?? null
}

/**
 * 决定登录后要不要改模型，以及改成什么。
 *
 * `current` 是 Hermes 当前配置里的主模型（没有则传 null/空串）。
 */
export function decideModelSelection(
  granted: readonly string[],
  current: string | null | undefined,
): ModelSelectionDecision {
  const first = firstGrantedModel(granted)

  // 一个都没开通：不选、也不能让他进聊天界面
  if (!first) {
    return { model: null, reason: 'no-grants' }
  }

  const normalized = String(current || '').trim()

  if (!normalized) {
    return { model: first, reason: 'selected' }
  }

  // 已选的模型后来被管理员收回授权 —— 客户端还记着它，用户下次发消息会撞
  // model_not_allowed。退回第一个可用的，比让他自己去发现要好
  if (!granted.some(name => String(name || '').trim() === normalized)) {
    return { model: first, reason: 'regranted' }
  }

  return { model: null, reason: 'kept' }
}

export interface ApplyModelOptions {
  baseUrl: string
  token: string
  providerId: string
  model: string
  fetchImpl?: typeof fetch
}

/**
 * 让 Hermes 把模型写进它自己的配置。
 *
 * 失败不抛：登录已经成功，模型没选上顶多是用户要自己挑一次，
 * 不该把整个登录回滚掉。调用方据返回值决定要不要提示。
 */
export async function applyMainModel({
  baseUrl,
  token,
  providerId,
  model,
  fetchImpl = fetch,
}: ApplyModelOptions): Promise<boolean> {
  try {
    const response = await fetchImpl(`${baseUrl.replace(/\/+$/, '')}/api/model/set`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ scope: 'main', provider: providerId, model }),
      signal: AbortSignal.timeout(15_000),
    })

    return response.ok
  } catch {
    return false
  }
}

/** 从网关 `/v1/models` 的响应里取出模型名清单。 */
export function grantedModelsFromCatalog(payload: unknown): string[] {
  const data = (payload as { data?: unknown })?.data

  if (!Array.isArray(data)) {
    return []
  }

  return data
    .map(entry => String((entry as { id?: unknown })?.id || '').trim())
    .filter(Boolean)
}

/** 发送前重查的结果。 */
export type SendGateOutcome =
  | { status: 'ready'; model: string; replacedFrom?: string }
  | { status: 'no-grants' }
  | { status: 'unavailable' }

/**
 * 发消息前确认有可用模型；没有就**当场重查一次**再决定。
 *
 * 解决的是这个真实场景：管理员刚给他开通了模型，而客户端还记着登录时那份
 * 空清单。没有这次重查，用户会遇到「明明开了权限怎么还发不出去」，
 * 而正确操作是重新登录 —— 那是只有我们自己知道的隐性知识。
 *
 * 三种结局必须分开，尤其是后两种：重查失败（网关连不上）和确实没开通是
 * 两回事。把网络抖动说成「联系管理员开通」，他找过去管理员一看权限明明有，
 * 两边都白跑一趟。
 */
export async function resolveModelForSend(options: {
  currentModel: string | null | undefined
  fetchGranted: () => Promise<string[] | null>
  applyModel: (model: string) => Promise<boolean>
}): Promise<SendGateOutcome> {
  const current = String(options.currentModel || '').trim()

  // null 表示这次重查本身失败了（网络/网关），不是「查到了但为空」
  const granted = await options.fetchGranted()

  if (current) {
    // 已经选过模型，也要核对它还在不在授权清单里（2026-09-18）。原先这里直接
    // 放行：默认模型只在登录那一刻核对过一次，管理员之后下架了它，已登录的人
    // 每开一个新对话都撞 403 model_not_allowed，而退出重登才会换 —— 又是只有
    // 我们自己知道的隐性知识。网关往返由调用方做短时缓存，不是每条消息一次。
    //
    // 问不到清单时照常放行：把网络抖动当成「你的模型被收回了」而把人换走，
    // 比让这一条发送撞一次网关自己的报错更糟。
    if (granted === null) {
      return { status: 'ready', model: current }
    }

    const decision = decideModelSelection(granted, current)

    if (decision.reason === 'no-grants') {
      return { status: 'no-grants' }
    }

    if (!decision.model) {
      return { status: 'ready', model: current }
    }

    // 写不进配置就不假装换好了：照旧放行，由网关给出它自己的报错
    return (await options.applyModel(decision.model))
      ? { status: 'ready', model: decision.model, replacedFrom: current }
      : { status: 'ready', model: current }
  }

  if (granted === null) {
    return { status: 'unavailable' }
  }

  const decision = decideModelSelection(granted, null)

  if (!decision.model) {
    return { status: 'no-grants' }
  }

  const applied = await options.applyModel(decision.model)

  return applied ? { status: 'ready', model: decision.model } : { status: 'unavailable' }
}
