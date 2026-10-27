/**
 * windows-git-runtime.ts
 *
 * Finds (or silently installs) the Git Bash the agent's shell tool runs on.
 *
 * Until 0.18.2 this was a PowerShell script, `resources/windows-git/ensure-git.ps1`.
 * That made `powershell.exe` a hard dependency of *every* launch — Git is checked
 * on each boot, not just the first — and PowerShell is the first thing corporate
 * antivirus refuses to let an unknown app start. On 2026-09-20 a user's machine
 * refused it (360) and the client could not boot at all: no amount of manually
 * pre-installing Git would have helped, because the detection itself lived inside
 * the script that could not run. Everything here is now plain child processes,
 * each one named in its error if Windows refuses it.
 *
 * Concurrency: the script held a named mutex. Boot serializes through
 * `desktopRuntimeOperation` in main.ts and the app is single-instance, so the
 * installer cannot race itself.
 */

import { execFile } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

import { decorateSpawnFailure, isBlockedSpawn } from './windows-spawn-failure'

const exec = promisify(execFile)

export interface GitRuntime {
  root: string
  bash: string
  installed: boolean
}

interface RunOptions {
  timeout?: number
  env?: NodeJS.ProcessEnv
}

/** Inno Setup answers for the same BashOnly install the script performed. */
const INSTALLER_FLAGS = [
  '/VERYSILENT',
  '/SUPPRESSMSGBOXES',
  '/NORESTART',
  '/SP-',
  '/NOCLOSEAPPLICATIONS',
  '/NORESTARTAPPLICATIONS',
  '/NOICONS',
  '/COMPONENTS=',
  '/o:PathOption=BashOnly',
  '/o:UseCredentialManager=Disabled'
]

// Proves the shell can actually run: bash itself, a coreutils binary, and git.
const HEALTH_COMMAND = '/usr/bin/true && /usr/bin/cat --version >/dev/null && git --version >/dev/null'

function system32(name: string) {
  return path.win32.join(process.env.SystemRoot || 'C:\\Windows', 'System32', name)
}

async function run(file: string, args: string[], options: RunOptions = {}) {
  try {
    return await exec(file, args, { windowsHide: true, encoding: 'utf8', ...options })
  } catch (error) {
    throw decorateSpawnFailure(file, error)
  }
}

function isFile(candidate: string) {
  try {
    return fs.statSync(candidate).isFile()
  } catch {
    return false
  }
}

async function sha256(file: string) {
  const digest = crypto.createHash('sha256')

  for await (const chunk of fs.createReadStream(file)) {
    digest.update(chunk)
  }

  return digest.digest('hex')
}

/** Install roots worth trying, in the order the PowerShell helper used. */
async function knownRoots() {
  const roots: string[] = []
  const bash = process.env.HERMES_GIT_BASH_PATH

  if (bash) {
    // Covers both real layouts: <root>\bin\bash.exe and <root>\usr\bin\bash.exe.
    const binDirectory = path.win32.dirname(bash)

    roots.push(path.win32.dirname(binDirectory), path.win32.dirname(path.win32.dirname(binDirectory)))
  }

  roots.push(
    path.win32.join(process.env.LOCALAPPDATA || '', 'hermes', 'git'),
    path.win32.join(process.env.LOCALAPPDATA || '', 'Programs', 'Git'),
    path.win32.join(process.env.ProgramFiles || '', 'Git'),
    path.win32.join(process.env.ProgramW6432 || '', 'Git'),
    path.win32.join(process.env['ProgramFiles(x86)'] || '', 'Git')
  )

  return roots.filter(root => root && path.win32.isAbsolute(root))
}

/** Git for Windows records its install path; read it without a script host. */
async function registeredRoots() {
  const roots: string[] = []

  for (const key of ['HKCU\\Software\\GitForWindows', 'HKLM\\Software\\GitForWindows']) {
    try {
      const { stdout } = await run(system32('reg.exe'), ['query', key, '/v', 'InstallPath'], { timeout: 5_000 })
      const match = /InstallPath\s+REG_SZ\s+(.+)/i.exec(String(stdout))

      if (match) {
        roots.push(match[1].trim())
      }
    } catch {
      /* Not registered, or reg.exe is refused as well; other candidates remain. */
    }
  }

  return roots
}

/** A Git already on PATH lives at <root>\cmd\git.exe. */
async function pathRoots() {
  try {
    const { stdout } = await run(system32('where.exe'), ['git.exe'], { timeout: 5_000 })

    return String(stdout)
      .trim()
      .split(/\r?\n/)
      .map(entry => entry.trim())
      .filter(entry => entry && !/\\WindowsApps\\/i.test(entry))
      .map(entry => path.win32.dirname(path.win32.dirname(entry)))
  } catch {
    return []
  }
}

async function verifyRoot(root: string, refused: Set<string>): Promise<GitRuntime | null> {
  const bash = path.win32.join(root, 'bin', 'bash.exe')

  if (!isFile(bash) || !isFile(path.win32.join(root, 'cmd', 'git.exe'))) {
    return null
  }

  try {
    await run(bash, ['--noprofile', '--norc', '-c', HEALTH_COMMAND], {
      timeout: 15_000,
      env: {
        ...process.env,
        ...gitRuntimeEnvironment({ root, bash, installed: false }, process.env.PATH || ''),
        MSYS2_PATH_TYPE: 'inherit'
      }
    })

    return { root, bash, installed: false }
  } catch (error) {
    // A refused bash.exe is not "Git is missing" — remember it, or the user is
    // told to reinstall something that is already sitting on disk.
    if (isBlockedSpawn(error)) {
      refused.add(bash)
    }

    return null
  }
}

async function installGit(resourcesPath: string, installerPath: string): Promise<GitRuntime> {
  const dependency = JSON.parse(
    fs.readFileSync(path.join(resourcesPath, 'windows-git', 'dependency.json'), 'utf8')
  ) as { filename: string; sha256: string; size: number }

  const installer = installerPath || path.join(resourcesPath, 'windows-git', dependency.filename)

  if (!isFile(installer)) {
    throw new Error('没有找到校验过的 Git 安装包，请重新运行环境修复。')
  }

  if (
    fs.statSync(installer).size !== dependency.size ||
    (await sha256(installer)) !== dependency.sha256.toLowerCase()
  ) {
    throw new Error('Git 安装包 SHA-256 校验失败，已取消安装。')
  }

  const target = path.win32.join(process.env.LOCALAPPDATA || '', 'Programs', 'Git')
  const logDirectory = path.win32.join(process.env.LOCALAPPDATA || '', 'hermes', 'logs')

  fs.mkdirSync(logDirectory, { recursive: true })

  const log = path.win32.join(logDirectory, 'git-install.log')

  try {
    await run(installer, [...INSTALLER_FLAGS, `/DIR=${target}`, `/LOG=${log}`], { timeout: 300_000 })
  } catch (error) {
    if (isBlockedSpawn(error)) {
      throw error
    }

    // 3010 = installed, reboot pending. Anything else is a real failure.
    if ((error as { code?: unknown }).code !== 3010) {
      throw new Error(`Git 安装失败（${(error as Error).message}）。安装日志：${log}`, { cause: error })
    }
  }

  const runtime = await verifyRoot(target, new Set())

  if (!runtime) {
    throw new Error(`Git 已安装但终端命令无法执行。安装日志：${log}`)
  }

  return { ...runtime, installed: true }
}

/**
 * Resolve a working Git Bash, installing the verified offline package when
 * `mode` is 'Ensure'. 'Check' never writes anything.
 */
export async function ensureWindowsGit(
  resourcesPath: string,
  mode: 'Check' | 'Ensure' = 'Ensure',
  installerPath = ''
): Promise<GitRuntime> {
  const refused = new Set<string>()

  try {
    const seen = new Set<string>()

    // Grouped and lazy: the usual machine matches a known directory on the first
    // group, so reg.exe and where.exe are never started at all.
    for (const group of [knownRoots, registeredRoots, pathRoots]) {
      for (const root of await group()) {
        const key = root.toLowerCase()

        if (seen.has(key)) {
          continue
        }

        seen.add(key)

        const runtime = await verifyRoot(root, refused)

        if (runtime) {
          return runtime
        }
      }
    }

    if (mode === 'Check') {
      throw refused.size
        ? Object.assign(new Error(`无法启动 ${[...refused][0]}：被安全软件或组策略拦截（EPERM）`), {
            blockedSpawn: true
          })
        : new Error('没有找到可用的 Git Bash。')
    }

    return await installGit(resourcesPath, installerPath)
  } catch (error) {
    throw Object.assign(new Error(`终端环境准备失败：${(error as Error).message}`, { cause: error }), {
      blockedSpawn: isBlockedSpawn(error) || undefined
    })
  }
}

export function gitRuntimeEnvironment(runtime: GitRuntime, currentPath = '') {
  return {
    HERMES_GIT_BASH_PATH: runtime.bash,
    PATH: [
      path.win32.join(runtime.root, 'cmd'),
      path.win32.join(runtime.root, 'mingw64', 'bin'),
      path.win32.join(runtime.root, 'usr', 'bin'),
      currentPath
    ]
      .filter(Boolean)
      .join(';')
  }
}
