/** MemberPicker 的纯逻辑部分，单独拆出来便于测试 */

import type { MemberHit } from '@/api/members'

export function memberLabel (member: Pick<MemberHit, 'display_name' | 'username' | 'dept_path'>): string {
  const account = member.username && member.username !== member.display_name ? `（${member.username}）` : ''
  return `${member.display_name}${account}`
}

/**
 * 下拉候选 = 已选中的 + 本次搜索结果，按 uid 去重，已选的排前面。
 *
 * 不把已选的并进来的话，换一个关键字搜索时，已选的人从 items 里消失，
 * v-autocomplete 的 chip 就只剩 uid 一串十六进制。
 */
export function mergeMemberOptions (selected: MemberHit[], results: MemberHit[]): MemberHit[] {
  const seen = new Set<string>()
  const out: MemberHit[] = []
  for (const member of [...selected, ...results]) {
    if (!seen.has(member.uid)) {
      seen.add(member.uid)
      out.push(member)
    }
  }
  return out
}

/** 已选 uid 里哪些还没有展示信息，需要按 uid 回查 */
export function missingMemberUids (uids: string[], known: MemberHit[]): string[] {
  const have = new Set(known.map(m => m.uid))
  return [...new Set(uids.filter(uid => uid && !have.has(uid)))]
}
