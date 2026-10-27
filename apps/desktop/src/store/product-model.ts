import { atom } from 'nanostores'

import type { ProductModelSendGate, ProductModelSync } from '@/global'
import { translateNow } from '@/i18n'
import { IS_BRANDED_PRODUCT } from '@/lib/product-policy'
import { dismissNotification, notify } from '@/store/notifications'
import { isProductGatewayUnreachable } from '@/store/product-gateway'

/**
 * 品牌客户端的"当前有没有可用模型"（阶段 4 · 决策 #50、#51）。
 *
 * ## 为什么它必须是多态而不是布尔
 *
 * `no-grants`（管理员确实没给他开通）和 `unavailable`（我们没问出来 —— 网关连
 * 不上、写配置失败）**必须分开**。把网络抖动说成"联系管理员开通"，他找过去，
 * 管理员一看权限明明有，两边都白跑一趟；反过来把没开通说成"稍后重试"，他会一直
 * 重试到放弃。
 *
 * `unknown` 是"还没问过"：应用重启后会话是从磁盘恢复的，登录流程根本没跑，
 * 这时既不能报平安也不能报警。
 *
 * ## 为什么不禁用输入框
 *
 * 没有可用模型的人照样可能只是来翻历史对话的。连字都打不了会变成另一类投诉，
 * 而且比"发不出去"更难解释。所以只提示、不封锁 —— 真正的闸门在发送那一步
 * （`ensureModelBeforeSend`），并且那一步还会**再查一次**。
 */
export type ProductModelAvailability = 'no-grants' | 'ready' | 'unavailable' | 'unknown'

export const $productModelAvailability = atom<ProductModelAvailability>('unknown')

/** 横幅用固定 id：重复提示是替换而不是叠一摞。 */
const BANNER_ID = 'product-model-availability'

/**
 * 把登录时那次选型的结果翻译成横幅状态。
 *
 * `apply-failed` 归到 `unavailable` 而不是 `ready`：报 ready 的话消息照样发出去
 * 再撞一次 `HTTP 400: 缺少 model 参数`，等于把一次明确的失败变成一次含糊的失败。
 */
export function availabilityFromSync(sync: ProductModelSync | null | undefined): ProductModelAvailability {
  if (sync?.reason === 'no-grants') {
    return 'no-grants'
  }

  if (sync?.reason === 'apply-failed' || sync?.reason === 'unavailable') {
    return 'unavailable'
  }

  return 'ready'
}

/** 把闸门结果翻译成横幅状态。 */
export function availabilityFromGate(gate: ProductModelSendGate): ProductModelAvailability {
  return gate.status === 'ready' ? 'ready' : gate.status
}

/**
 * 写入状态并同步横幅。
 *
 * 恢复正常时**主动收掉**横幅：管理员刚开通完，用户手里还挂着一条"请联系管理员
 * 开通"，他会以为没生效又去催一遍。
 */
export function setProductModelAvailability(next: ProductModelAvailability): void {
  $productModelAvailability.set(next)

  if (next === 'ready' || next === 'unknown') {
    dismissNotification(BANNER_ID)

    return
  }

  // 网关整个连不上的时候，「暂时无法确认可用模型」只是那件事的下游症状。
  // 两条横幅一起挂着，用户得自己判断哪条是根因 —— 那不是用户该做的事。
  // 状态照常写（发送闸门仍然拦住），只是不再多说一遍
  if (next === 'unavailable' && isProductGatewayUnreachable()) {
    dismissNotification(BANNER_ID)

    return
  }

  notify({
    id: BANNER_ID,
    // durationMs: 0 —— 这不是一次性反馈，是一个持续存在的状态；自动消失的话
    // 用户按发送时早就看不到解释了
    durationMs: 0,
    kind: 'warning',
    message: translateNow(next === 'no-grants' ? 'productModel.noGrants' : 'productModel.unavailable')
  })
}

/** 退出登录时清干净，别让上一个账号的状态挂到下一个账号头上。 */
export function resetProductModelAvailability(): void {
  setProductModelAvailability('unknown')
}

/**
 * 问一次主进程：现在有没有可用模型？顺带把横幅同步好。
 *
 * 非品牌构建、以及 preload 还没跟上的开发期热更新窗口，一律**放行**。stock
 * Hermes 没有托管模型这回事，误挡是我们凭空造出来的故障；真发不出去的话网关
 * 自己会报错。
 */
export async function refreshProductModelAvailability(): Promise<ProductModelAvailability> {
  const gate = await queryProductModelGate()

  return gate ? availabilityFromGate(gate) : 'ready'
}

/**
 * 问主进程要一次闸门结果并同步横幅；非品牌构建 / preload 未就绪返回 `null`（放行）。
 */
async function queryProductModelGate(): Promise<null | ProductModelSendGate> {
  const ensure = window.hermesDesktop?.productModel?.ensure

  if (!IS_BRANDED_PRODUCT || !ensure) {
    setProductModelAvailability('ready')

    return null
  }

  let gate: ProductModelSendGate

  try {
    gate = await ensure()
  } catch {
    gate = { status: 'unavailable' }
  }

  setProductModelAvailability(availabilityFromGate(gate))

  return gate
}

/**
 * 发送前的闸门。放行返回 `true`。
 *
 * 每次发送都问一次主进程：它会核对配置里的默认模型还在不在授权清单里（网关往返
 * 在主进程缓存 60 秒，这里只是一次本机 IPC）。原先「已确认有模型」时纯本地放行，
 * 于是登录后被管理员下架的默认模型永远不会被发现 —— 2026-09-18 所有内测用户的
 * 新对话都撞 403 `model_not_allowed`，而唯一的解法是退出重登。
 *
 * 状态存疑（登录时没开通、上次没问出来、刚重启）时这一次重查同样会把模型选上：
 * 管理员刚开通的话照常发出去，用户不需要知道「要重新登录才生效」。
 */
export async function ensureModelBeforeSend(): Promise<boolean> {
  if (!IS_BRANDED_PRODUCT) {
    return true
  }

  const gate = await queryProductModelGate()

  if (gate && gate.status !== 'ready') {
    return false
  }

  // 已选模型还在不在？「有可用模型」和「我这个会话/新对话选的那个还允许用」是
  // 两个问题，上面只回答了第一个。交给注册进来的对账函数（它持有会话上下文和
  // 模型目录缓存，还会自己限流）；闸门结果一并交过去，新对话据此对齐默认模型。
  return (await sessionModelReconciler?.(null, gate)) ?? true
}

/**
 * 会话级模型对账的注入点。
 *
 * 对账本身要读 react-query 缓存、改会话状态、发 `config.set`，那些都长在
 * `useModelControls` 里；而调用时机在这个 store。与其把 store 塞进 hook，不如
 * 让 hook 在挂载时把自己登记进来 —— 没登记（stock Hermes、测试、preload 还没
 * 跟上的热更新窗口）就是放行，和这个文件其余部分的降级策略一致。
 */
export type SessionModelReconciler = (sessionId?: null | string, gate?: null | ProductModelSendGate) => Promise<boolean>

let sessionModelReconciler: null | SessionModelReconciler = null

export function setSessionModelReconciler(fn: null | SessionModelReconciler): void {
  sessionModelReconciler = fn
}
