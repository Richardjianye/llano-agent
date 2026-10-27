import crypto from 'node:crypto'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ensureWindowsGit, gitRuntimeEnvironment } from './windows-git-runtime'

const INSTALLER = 'C:\\home\\runtime\\downloads\\payload.exe'
const INSTALLER_BYTES = Buffer.from('git-for-windows installer')
const INSTALLER_SHA = crypto.createHash('sha256').update(INSTALLER_BYTES).digest('hex')
const PROGRAM_FILES_GIT = 'C:\\Program Files\\Git'
const INSTALL_TARGET = 'C:\\Users\\l\\AppData\\Local\\Programs\\Git'

// A mocked execFile has no promisify.custom symbol, so promisify() would resolve
// with stdout alone instead of { stdout, stderr }. Supply the symbol, or the
// module under test sees a shape production never produces.
const exec = vi.hoisted(() => {
  const mock = vi.fn()

  ;(mock as unknown as Record<symbol, unknown>)[Symbol.for('nodejs.util.promisify.custom')] = (
    file: string,
    args: string[],
    options: unknown
  ) =>
    new Promise((resolve, reject) => {
      mock(file, args, options, (error: unknown, stdout = '', stderr = '') =>
        error ? reject(error) : resolve({ stdout, stderr })
      )
    })

  return mock
})

const present = vi.hoisted(() => new Set<string>())

// The mock itself must be the export, not a wrapper: promisify() looks for the
// custom symbol on the function it is handed.
vi.mock('node:child_process', () => ({ execFile: exec }))
vi.mock('node:fs', () => ({
  default: {
    statSync: (file: string) => {
      if (!present.has(file)) {
        throw Object.assign(new Error(`ENOENT: ${file}`), { code: 'ENOENT' })
      }

      return { isFile: () => true, size: INSTALLER_BYTES.length }
    },
    readFileSync: () =>
      JSON.stringify({ version: '2.55.0.5', filename: 'Git.exe', sha256: INSTALLER_SHA, size: INSTALLER_BYTES.length }),
    mkdirSync: vi.fn(),
    createReadStream: () =>
      (async function* () {
        yield INSTALLER_BYTES
      })()
  }
}))

function installGit(root: string) {
  present.add(`${root}\\bin\\bash.exe`)
  present.add(`${root}\\cmd\\git.exe`)
}

/** Nothing found, nothing refused: reg.exe/where.exe answer empty. */
function noDiscovery(file: string, callback: (error: unknown, stdout?: string) => void) {
  if (/reg\.exe$/i.test(file) || /where\.exe$/i.test(file)) {
    callback(Object.assign(new Error('exit 1'), { code: 1 }))

    return true
  }

  return false
}

beforeEach(() => {
  process.env.SystemRoot = 'C:\\Windows'
  process.env.LOCALAPPDATA = 'C:\\Users\\l\\AppData\\Local'
  process.env.ProgramFiles = 'C:\\Program Files'
  delete process.env.ProgramW6432
  delete process.env['ProgramFiles(x86)']
  delete process.env.HERMES_GIT_BASH_PATH
})

afterEach(() => {
  exec.mockReset()
  present.clear()
})

describe('Windows Git preparation without a script host', () => {
  it('accepts an installed Git Bash without starting PowerShell', async () => {
    installGit(PROGRAM_FILES_GIT)
    exec.mockImplementation((file: string, args: string[], options: { env?: NodeJS.ProcessEnv }, callback: never) => {
      if (noDiscovery(file, callback)) {
        return
      }

      expect(file).toBe(`${PROGRAM_FILES_GIT}\\bin\\bash.exe`)
      expect(args).toContain('--noprofile')
      expect(options.env?.PATH?.split(';')[0]).toBe(`${PROGRAM_FILES_GIT}\\cmd`)
      expect(options.env?.MSYS2_PATH_TYPE).toBe('inherit')
      ;(callback as unknown as (e: unknown, o: string, s: string) => void)(null, '', '')
    })

    // 0.18.2 检查 Git 走的是 ensure-git.ps1，于是被 360 拦掉 powershell.exe 的机器
    // 连「Git 其实已经装好了」都发现不了，每次启动都失败。
    expect(await ensureWindowsGit('C:\\app\\resources', 'Check')).toEqual({
      root: PROGRAM_FILES_GIT,
      bash: `${PROGRAM_FILES_GIT}\\bin\\bash.exe`,
      installed: false
    })
    expect(exec.mock.calls.some(call => /powershell/i.test(String(call[0])))).toBe(false)
  })

  it('reads the registry and PATH only when no known directory answers', async () => {
    installGit('D:\\Tools\\Git')
    exec.mockImplementation((file: string, _args: string[], _options: unknown, callback: never) => {
      const done = callback as unknown as (e: unknown, o?: string, s?: string) => void

      if (/reg\.exe$/i.test(file)) {
        done(
          null,
          '\r\nHKEY_CURRENT_USER\\Software\\GitForWindows\r\n    InstallPath    REG_SZ    D:\\Tools\\Git\r\n',
          ''
        )

        return
      }

      if (/where\.exe$/i.test(file)) {
        done(Object.assign(new Error('exit 1'), { code: 1 }))

        return
      }

      done(null, '', '')
    })

    expect((await ensureWindowsGit('C:\\app\\resources', 'Check')).root).toBe('D:\\Tools\\Git')
    // where.exe is never reached once the registry answers.
    expect(exec.mock.calls.some(call => /where\.exe$/i.test(String(call[0])))).toBe(false)
  })

  it('reports a refused bash.exe as interference, not as a missing Git', async () => {
    installGit(PROGRAM_FILES_GIT)
    exec.mockImplementation((file: string, _args: string[], _options: unknown, callback: never) => {
      const done = callback as unknown as (e: unknown) => void

      if (noDiscovery(file, done as never)) {
        return
      }

      done(Object.assign(new Error('spawn EPERM'), { code: 'EPERM' }))
    })

    // Telling this machine to reinstall Git would be a dead end: it is already
    // on disk, antivirus just refuses to start it.
    await expect(ensureWindowsGit('C:\\app\\resources', 'Check')).rejects.toThrow('被安全软件或组策略拦截')
    await expect(ensureWindowsGit('C:\\app\\resources', 'Check')).rejects.toThrow(`${PROGRAM_FILES_GIT}\\bin\\bash.exe`)
  })

  it('says plainly when no Git is installed at all', async () => {
    exec.mockImplementation((file: string, _args: string[], _options: unknown, callback: never) => {
      noDiscovery(file, callback as never)
    })

    await expect(ensureWindowsGit('C:\\app\\resources', 'Check')).rejects.toThrow('没有找到可用的 Git Bash')
  })

  it('installs by running the verified installer directly, reboot-pending included', async () => {
    present.add(INSTALLER)
    const installs: string[][] = []
    exec.mockImplementation((file: string, args: string[], _options: unknown, callback: never) => {
      const done = callback as unknown as (e: unknown, o?: string, s?: string) => void

      if (noDiscovery(file, done as never)) {
        return
      }

      if (file === INSTALLER) {
        installs.push(args)
        installGit(INSTALL_TARGET)
        // 3010 = installed, reboot pending; the old script accepted it too.
        done(Object.assign(new Error('exit 3010'), { code: 3010 }))

        return
      }

      done(null, '', '')
    })

    expect(await ensureWindowsGit('C:\\app\\resources', 'Ensure', INSTALLER)).toEqual({
      root: INSTALL_TARGET,
      bash: `${INSTALL_TARGET}\\bin\\bash.exe`,
      installed: true
    })
    expect(installs).toHaveLength(1)
    expect(installs[0]).toContain('/VERYSILENT')
    expect(installs[0]).toContain('/o:PathOption=BashOnly')
    expect(installs[0]).toContain(`/DIR=${INSTALL_TARGET}`)
    expect(installs[0].some(arg => arg.startsWith('/LOG='))).toBe(true)
    expect(exec.mock.calls.some(call => /powershell/i.test(String(call[0])))).toBe(false)
  })

  it('refuses to run an installer whose bytes do not match the manifest', async () => {
    present.add(INSTALLER)
    const started: string[] = []
    exec.mockImplementation((file: string, _args: string[], _options: unknown, callback: never) => {
      started.push(file)
      noDiscovery(file, callback as never)
    })

    // Same guard the script had: size and SHA-256 before anything executes.
    await expect(
      ensureWindowsGit('C:\\app\\resources', 'Ensure', 'C:\\home\\runtime\\downloads\\other.exe')
    ).rejects.toThrow('没有找到校验过的 Git 安装包')
    expect(started.some(file => file.endsWith('.exe') && !/(reg|where)\.exe$/i.test(file))).toBe(false)
  })

  it('surfaces a refused installer instead of blaming the installation', async () => {
    present.add(INSTALLER)
    exec.mockImplementation((file: string, _args: string[], _options: unknown, callback: never) => {
      const done = callback as unknown as (e: unknown) => void

      if (noDiscovery(file, done as never)) {
        return
      }

      done(Object.assign(new Error('spawn EPERM'), { code: 'EPERM' }))
    })

    await expect(ensureWindowsGit('C:\\app\\resources', 'Ensure', INSTALLER)).rejects.toThrow('被安全软件或组策略拦截')
  })
})

it('exports the same PATH shape the runtime manager inherits', () => {
  const runtime = { root: 'C:\\Users\\测试\\Git', bash: 'C:\\Users\\测试\\Git\\bin\\bash.exe', installed: true }
  const env = gitRuntimeEnvironment(runtime, 'C:\\Windows\\System32')

  expect(env.HERMES_GIT_BASH_PATH).toBe(runtime.bash)
  expect(env.PATH.split(';')[0]).toBe(`${runtime.root}\\cmd`)
  expect(env.PATH).toContain('C:\\Windows\\System32')
})
