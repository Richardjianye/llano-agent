/**
 * 品牌版的可选更新提示（CUSTOM）
 *
 * 上游用右上角一条通知提示「有更新」。员工反馈那一小块白框很容易被忽略，
 * 结果大家一直停在旧版本上，修过的问题还在他们机器上发生。品牌版改成带蒙版的
 * 模态框：一打开就看得见，但仍然可以「稍后提醒」—— 强制更新另有全屏阻断
 * （`product-mandatory-update.ts`），这里只管可选更新。
 *
 * 「稍后提醒」沿用上游那条通知的 24 小时冷却（`store/updates.ts`），两边共用
 * 一个冷却键，所以不会出现模态框关了、通知又冒出来的情况。
 */

import { atom } from 'nanostores'

import type { DesktopUpdateStatus } from '@/global'

/** 非 null 即显示模态框，值是触发它的那份已验签状态。 */
export const $optionalUpdatePrompt = atom<DesktopUpdateStatus | null>(null)

export function showOptionalUpdatePrompt(status: DesktopUpdateStatus): void {
  $optionalUpdatePrompt.set(status)
}

export function closeOptionalUpdatePrompt(): void {
  $optionalUpdatePrompt.set(null)
}
