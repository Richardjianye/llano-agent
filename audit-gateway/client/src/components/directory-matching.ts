import type { DirectoryAccount, DirectoryAction, DirectoryRow } from '@/api/directory'

export interface DirectoryChoice {
  action: DirectoryAction['action'] | null
  uid?: string
  username?: string
  dept_id?: number | null
}

/** Only suggest reviewed, active account options with a one-to-one name candidate. */
export function uniqueCandidateBindings (
  rows: DirectoryRow[], accounts: DirectoryAccount[],
  choices: Record<string, DirectoryChoice>, applied: Set<string>,
): DirectoryAction[] {
  const available = new Set(accounts.map(account => account.uid))
  const claims = new Map<string, number>()
  for (const row of rows) {
    if (!row.can_bind || applied.has(row.key)) {
      continue
    }
    for (const candidate of row.candidates) {
      claims.set(candidate.uid, (claims.get(candidate.uid) ?? 0) + 1)
    }
  }
  const occupied = new Set(Object.entries(choices)
    .filter(([key, choice]) => !applied.has(key) && choice.action === 'bind' && choice.uid)
    .map(([, choice]) => choice.uid))
  return rows.flatMap(row => {
    const candidate = row.candidates[0]
    if (row.category !== 'candidate' || !row.can_bind || applied.has(row.key)
      || choices[row.key]?.action || row.candidates.length !== 1 || !candidate
      || !available.has(candidate.uid) || claims.get(candidate.uid) !== 1 || occupied.has(candidate.uid)) {
      return []
    }
    return [{ key: row.key, action: 'bind' as const, uid: candidate.uid }]
  })
}

export interface DirectoryPlan {
  ready: DirectoryAction[]
  incomplete: Record<string, string>
  selected: number
  skipped: number
}

export function planDirectoryActions (
  rows: DirectoryRow[], accounts: DirectoryAccount[],
  choices: Record<string, DirectoryChoice>, applied: Set<string>,
): DirectoryPlan {
  const plan: DirectoryPlan = { ready: [], incomplete: {}, selected: 0, skipped: 0 }
  const available = new Set(accounts.map(account => account.uid))
  const claims = new Map<string, number>()
  for (const [key, choice] of Object.entries(choices)) {
    if (!applied.has(key) && choice.action === 'bind' && choice.uid) {
      claims.set(choice.uid, (claims.get(choice.uid) ?? 0) + 1)
    }
  }
  for (const row of rows) {
    if (applied.has(row.key)) {
      continue
    }
    const choice = choices[row.key]
    if (!choice?.action) {
      plan.skipped++
      continue
    }
    plan.selected++
    let issue = ''
    if (!row[`can_${choice.action}`]) {
      issue = '此记录不支持该操作，请暂不处理'
    } else if (choice.action === 'bind' && (!choice.uid || !available.has(choice.uid))) {
      issue = '请选择可绑定账号；高权限账号需本人登录绑定'
    } else if (choice.action === 'bind' && (claims.get(choice.uid!) ?? 0) > 1) {
      issue = '多个成员选择了同一账号，请分别核对'
    } else if (choice.action === 'create' && (!choice.username?.trim() || choice.username.trim().length > 36 || /\s/.test(choice.username.trim()))) {
      issue = '请填写 1–36 位、不含空格的用户名'
    } else if (choice.action === 'create' && choice.dept_id != null && !row.mapped_dept_ids.includes(choice.dept_id)) {
      issue = '请选择已核对的映射部门，或留空'
    }
    if (issue) {
      plan.incomplete[row.key] = issue
      continue
    }
    if (choice.action === 'bind') {
      plan.ready.push({ key: row.key, action: 'bind', uid: choice.uid })
    } else if (choice.action === 'create') {
      plan.ready.push({ key: row.key, action: 'create', username: choice.username!.trim(), dept_id: choice.dept_id })
    } else {
      plan.ready.push({ key: row.key, action: 'disable' })
    }
  }
  return plan
}

export function directoryBatches<T> (items: T[]): T[][] {
  const batches: T[][] = []
  for (let i = 0; i < items.length; i += 100) {
    batches.push(items.slice(i, i + 100))
  }
  return batches
}
