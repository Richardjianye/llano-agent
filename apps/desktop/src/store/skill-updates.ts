import { atom } from 'nanostores'
import { useEffect } from 'react'

import { getCompanySkillUpdateCount } from '@/hermes'

/**
 * CUSTOM（2026-10-16，P1）：有新版本的已装公司技能数，驱动侧栏「技能与工具」的小红点。
 *
 * 两个来源写它：侧栏的低频后台检查（启动后稍等、之后每 6 小时，只读批量指标、不取包），
 * 以及技能页拿到列表时的即时数 —— 员工点完更新，红点立刻跟着变，不必等下一轮检查。
 */
export const $companySkillUpdates = atom(0)

const FIRST_CHECK_MS = 30_000
const CHECK_INTERVAL_MS = 6 * 60 * 60_000

export function setCompanySkillUpdates(count: number) {
  $companySkillUpdates.set(Math.max(0, count))
}

export async function refreshCompanySkillUpdates() {
  try {
    setCompanySkillUpdates((await getCompanySkillUpdateCount()).count)
  } catch {
    // 后台检查失败只是少一个提示；保留上次的数，不打扰员工。
  }
}

/** 在常驻的侧栏里挂一次即可；资料档案切换时由 key 触发重新检查。 */
export function useCompanySkillUpdatePoll(key: string | null) {
  useEffect(() => {
    const first = window.setTimeout(() => void refreshCompanySkillUpdates(), FIRST_CHECK_MS)
    const every = window.setInterval(() => void refreshCompanySkillUpdates(), CHECK_INTERVAL_MS)

    return () => {
      window.clearTimeout(first)
      window.clearInterval(every)
    }
  }, [key])
}
