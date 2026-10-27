import type { SkillInfo } from '@/hermes'

/**
 * CUSTOM（2026-10-16，P2）：本机自有技能的发布提示。
 * 发布后又改过（多半是后台复盘补了步骤）比「值得分享」优先：同事手里的已经旧了。
 */
export function publishHintLabel(skill: SkillInfo, r: { changedBadge: string; suggestBadge: string }): null | string {
  const state = skill.publish_state

  if (state?.local_changed) {
    return r.changedBadge
  }

  return state?.suggest ? r.suggestBadge : null
}
