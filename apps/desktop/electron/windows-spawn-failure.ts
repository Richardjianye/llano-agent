/**
 * windows-spawn-failure.ts
 *
 * Node hands back a *nameless* error when Windows refuses to create a process.
 * For any errno other than ENOENT/EAGAIN/EMFILE/ENFILE, `ChildProcess.spawn`
 * throws synchronously with `errnoException(err, 'spawn')` — message
 * `spawn EPERM`, no file name, no `path` property, thrown before the caller's
 * callback ever exists. Only the async ENOENT path gets `spawn <file> ENOENT`.
 *
 * That nameless shape is exactly what antivirus interference looks like. On
 * 2026-09-20 a user's first launch reported seven components in a row as
 * `Error: spawn EPERM`; working out that 360 was refusing `powershell.exe`
 * took reasoning backwards from the two components that did NOT fail (a
 * single-file copy and a system browser — the only two that never start a
 * script interpreter). The next person's screenshot has to be enough on its
 * own, so every Windows runtime spawn is decorated here.
 */

/** Windows returns ERROR_ACCESS_DENIED for both, and Node maps it to either. */
const REFUSED = new Set(['EPERM', 'EACCES'])

const ERRNO = /^E[A-Z]{2,}$/

type SpawnError = Error & { code?: unknown; blockedSpawn?: boolean; spawnFile?: string }

function errnoCode(error: unknown): string {
  const code = (error as SpawnError | null)?.code

  return typeof code === 'string' && ERRNO.test(code) ? code : ''
}

/**
 * Was the process refused *before it ran*, rather than run and failed?
 *
 * Walks `cause` so a wrapped failure ("终端环境准备失败：…") still answers, and
 * stops at a small depth because these chains are built here, not inherited.
 */
export function isBlockedSpawn(error: unknown, depth = 4): boolean {
  if (!error || typeof error !== 'object') {
    return false
  }

  if ((error as SpawnError).blockedSpawn === true || REFUSED.has(errnoCode(error))) {
    return true
  }

  return depth > 0 ? isBlockedSpawn((error as { cause?: unknown }).cause, depth - 1) : false
}

/**
 * Name the executable Windows refused, and say what that usually means.
 *
 * An error from a process that actually *ran* (non-zero exit, timeout, maxBuffer)
 * is returned untouched: its own message and `stderr` explain more than a spawn
 * label would, and callers match on `code` for expected exit statuses.
 */
export function decorateSpawnFailure(file: string, error: unknown): Error {
  const code = errnoCode(error)

  if (!code) {
    return error instanceof Error ? error : new Error(String(error))
  }

  const blocked = REFUSED.has(code)

  const decorated: SpawnError = new Error(
    blocked ? `无法启动 ${file}：被安全软件或组策略拦截（${code}）` : `无法启动 ${file}：${code}`,
    { cause: error }
  )

  decorated.code = code
  decorated.spawnFile = file
  decorated.blockedSpawn = blocked

  return decorated
}

// Longest expansion first: %LOCALAPPDATA% sits inside %USERPROFILE%.
const PLACEHOLDERS = ['LOCALAPPDATA', 'APPDATA', 'ProgramFiles', 'USERPROFILE']

/**
 * Rewrites a real path back to its environment-variable form.
 *
 * The branded failure card deliberately keeps raw local paths off screen — these
 * screenshots get forwarded around, and an expanded path carries the person's
 * Windows account name. `%APPDATA%\LLANO Agent` is just as pasteable into an
 * antivirus trust-list dialog, and stays the same on every machine.
 */
export function collapseWindowsPath(target: string, env: NodeJS.ProcessEnv = process.env): string {
  if (!target) {
    return target
  }

  const candidates = PLACEHOLDERS.map(name => ({ name, value: (env[name] || '').replace(/[\\/]+$/, '') }))
    .filter(entry => entry.value)
    .sort((left, right) => right.value.length - left.value.length)

  for (const { name, value } of candidates) {
    if (target.toLowerCase() === value.toLowerCase()) {
      return `%${name}%`
    }

    if (target.toLowerCase().startsWith(`${value.toLowerCase()}\\`)) {
      return `%${name}%${target.slice(value.length)}`
    }
  }

  return target
}

/**
 * The self-service instructions for a refused spawn.
 *
 * Kept next to the detection so a boot failure can print the one thing that
 * actually fixes it. Company antivirus is 火绒; 360 arrives uninvited with
 * other 360 products, which is what bit the 2026-09-20 machine.
 */
export function securityBlockAdvice(paths: readonly string[]): string {
  const directories = paths.filter(Boolean).map(entry => `    ${entry}`)

  return [
    '环境准备被安全软件或组策略拦截（程序无法启动子进程）。',
    '请把下列目录加入杀毒软件的信任区/白名单，然后重新打开应用：',
    ...directories,
    '火绒：主界面 → 设置 → 信任区 → 添加目录（含子目录）。',
    '360：防护中心 → 设置 → 信任区 → 添加目录；拦截记录里也可直接选「恢复并信任」。',
    '全公司统一处理可由 IT 在杀毒软件控制中心下发信任区策略。'
  ].join('\n')
}
