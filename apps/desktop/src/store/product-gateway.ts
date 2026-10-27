import { atom } from 'nanostores'

import type { ProductGatewayFailureKind } from '@/global'
import { translateNow } from '@/i18n'
import { IS_BRANDED_PRODUCT } from '@/lib/product-policy'
import { dismissNotification, notify } from '@/store/notifications'

/**
 * 公司审计网关是否连得上（阶段 4 · CP5 故障矩阵第 1 格）。
 *
 * ## 这个 store 是补一个洞，不是加一个功能
 *
 * 审计网关挂掉时 **Hermes sidecar 活得好好的**，渲染层和它之间的 WebSocket
 * 一直是 open。于是 `use-gateway-boot` 那套重连循环根本不启动，45 秒升级、
 * 恢复面板、CONNECTING 让位 —— 一条都不会发生。人工验收时的现象是：等 60 秒
 * 什么都没有，界面停在对话页，直到发消息才撞一句英文的
 * `redaction policy expired`。
 *
 * **「Hermes 网关断开」和「审计网关离线」是两个故障。** 第一次写故障矩阵时把
 * 它们当成了一格，测试全绿，测的却是另一个。
 *
 * ## 为什么状态是三态
 *
 * `unknown` 是「还没问过」。启动瞬间就报「连不上」会在每次冷启动闪一下红，
 * 而那时候只是还没轮询到。
 *
 * ## 重试的上限在哪
 *
 * 这里不自己起轮询：登录门禁本来就每 60 秒问一次 `productAuth.status()`，
 * 复用它。固定节奏 + 可见状态 + 一个用户点得到的「重试」按钮，就是这一格
 * 要求的「有限重试与恢复入口」—— 没有出口的有限重试只是把死循环换成死界面。
 */
export type ProductGatewayReachability = 'reachable' | 'unknown' | 'unreachable'

export const $productGatewayReachable = atom<ProductGatewayReachability>('unknown')

const BANNER_ID = 'product-gateway-unreachable'

export function productGatewayFailureMessage(failureKind: ProductGatewayFailureKind | undefined): string {
  if (failureKind === 'timeout') {
    return translateNow('productGateway.slow')
  }

  if (failureKind === 'response') {
    return translateNow('productGateway.unavailable')
  }

  // Backward-compatible default for an older preload that only returns
  // gatewayReachable=false. It historically meant a connection failure.
  return translateNow('productGateway.unreachable')
}

/** 网关明确连不上时为真。`unknown` 不算 —— 还没问过不等于连不上。 */
export function isProductGatewayUnreachable(): boolean {
  return $productGatewayReachable.get() === 'unreachable'
}

/**
 * 记录一次可达性观测，并同步横幅。
 *
 * `onRetry` 是横幅上那个按钮要干的事（立刻重查一次），由调用方注入，
 * 这个 store 不该知道登录门禁的存在。
 */
export function setProductGatewayReachable(
  next: ProductGatewayReachability,
  options: { failureKind?: ProductGatewayFailureKind; onRetry?: () => void } = {}
): void {
  $productGatewayReachable.set(next)

  if (next !== 'unreachable') {
    dismissNotification(BANNER_ID)

    return
  }

  notify({
    id: BANNER_ID,
    // 持续状态而不是一次性反馈：自动消失的话，用户回到电脑前只会看到
    // 一个不动的界面和零条解释
    durationMs: 0,
    kind: 'warning',
    message: productGatewayFailureMessage(options.failureKind),
    ...(options.onRetry
      ? { action: { label: translateNow('productGateway.retry'), onClick: options.onRetry } }
      : {})
  })
}

/** 退出登录时清干净。 */
export function resetProductGatewayReachable(): void {
  setProductGatewayReachable('unknown')
}

/**
 * 把一次状态查询的结果喂进来。非品牌构建直接忽略 —— stock Hermes 没有公司网关
 * 这回事，凭空多一条横幅是我们造出来的故障。
 */
export function applyGatewayReachability(
  reachable: boolean | undefined,
  options: { failureKind?: ProductGatewayFailureKind; onRetry?: () => void } = {}
): void {
  if (!IS_BRANDED_PRODUCT) {
    return
  }

  // 老版本 preload 不带这个字段：当作「没问出来」，别把 undefined 读成 false
  if (reachable === undefined) {
    return
  }

  setProductGatewayReachable(reachable ? 'reachable' : 'unreachable', options)
}
