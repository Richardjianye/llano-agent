/**
 * 强制更新的阻断状态（阶段 5 · U4）
 *
 * ## 判据从哪里来
 *
 * 只认 `DesktopUpdateStatus.mandatory` —— 那是主进程**从已验签的清单自己算**
 * 出来的（`updateIsMandatory()`），不是网关下发的字段。渲染层不重算、也不
 * 从别的地方推断：多一处判定就多一处会与另外三份实现漂移的地方。
 *
 * ## 这个模块存在的唯一理由：闩锁规则
 *
 * `$updateStatus` 在检查失败时也会被写入（带 `error`），而那份状态里没有
 * `mandatory`。如果遮罩直接读 `$updateStatus.mandatory`，就会变成：
 *
 *   1. 客户端拿到 required 清单 → 全屏阻断
 *   2. 半小时后的轮询碰上网络抖动 → 状态被换成 `{ error: 'check-failed' }`
 *   3. 遮罩消失，业务照常用
 *
 * 也就是「断网即可绕过强制更新」。所以规则是：
 *
 * - **只有检查成功的结果才能改变阻断状态**；失败的结果一律忽略；
 * - 成功且 `mandatory === true` → 阻断；
 * - 成功且不强制 → 解除（版本已追上，或管理员把那一版下架了）。
 *
 * ## 这道遮罩不是安全边界
 *
 * 它拦的是「用户没注意到必须更新」，不是「用户铁了心要用旧版」—— 渲染层的
 * 任何东西都可以被绕过。旧客户端真正用不了，靠的是网关那一侧对每个请求的
 * 鉴权与授权。这里只负责让人**看得见**、并且给出唯一的出路。
 */

import { atom } from 'nanostores'

import type { DesktopUpdateStatus } from '@/global'

/** 非 null 即表示业务必须被全屏阻断，值是触发阻断的那份已验签状态。 */
export const $mandatoryUpdate = atom<DesktopUpdateStatus | null>(null)

/**
 * 把一次更新检查的结果并入阻断状态。
 *
 * 检查失败（`status.error` 非空）时**保持原样**：一次取不到清单不代表不需要
 * 强制更新，上一次已验签的结论仍然有效。
 */
export function ingestUpdateStatusForBlocking(status: DesktopUpdateStatus | null): void {
  if (!status || status.error) {
    return
  }

  $mandatoryUpdate.set(status.mandatory === true ? status : null)
}

/** 仅供测试与登出重置使用。正常流程里状态只由 `ingestUpdateStatusForBlocking` 改。 */
export function resetMandatoryUpdate(): void {
  $mandatoryUpdate.set(null)
}
