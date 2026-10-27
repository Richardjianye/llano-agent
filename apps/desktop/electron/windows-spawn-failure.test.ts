import { describe, expect, it } from 'vitest'

import { collapseWindowsPath, decorateSpawnFailure, isBlockedSpawn, securityBlockAdvice } from './windows-spawn-failure'

/** The exact shape Node throws when Windows refuses CreateProcess. */
function refused(code = 'EPERM') {
  return Object.assign(new Error(`spawn ${code}`), { code, errno: -4048, syscall: 'spawn' })
}

describe('decorateSpawnFailure', () => {
  it('names the executable Node left out of a refusal', () => {
    const error = decorateSpawnFailure('C:\\Windows\\System32\\tar.exe', refused())

    // The 0.18.2 field report was seven lines of bare "Error: spawn EPERM";
    // nothing said which binary, so nothing said what to whitelist.
    expect(error.message).toBe('无法启动 C:\\Windows\\System32\\tar.exe：被安全软件或组策略拦截（EPERM）')
    expect(isBlockedSpawn(error)).toBe(true)
    expect((error as { spawnFile?: string }).spawnFile).toBe('C:\\Windows\\System32\\tar.exe')
  })

  it('reports a missing executable without blaming antivirus', () => {
    const error = decorateSpawnFailure('C:\\python\\python.exe', refused('ENOENT'))

    expect(error.message).toBe('无法启动 C:\\python\\python.exe：ENOENT')
    expect(isBlockedSpawn(error)).toBe(false)
  })

  it('leaves a process that ran and failed untouched, stderr and exit code included', () => {
    const failure = Object.assign(new Error('Command failed: installer.exe'), { code: 3010, stderr: 'reboot' })

    const timeout = Object.assign(new Error('timed out'), { killed: true })

    expect(decorateSpawnFailure('installer.exe', failure)).toBe(failure)
    expect(decorateSpawnFailure('installer.exe', timeout)).toBe(timeout)
  })
})

describe('isBlockedSpawn', () => {
  it('sees through the wrapping a caller adds for the user', () => {
    const wrapped = new Error('终端环境准备失败：无法启动 powershell.exe', {
      cause: decorateSpawnFailure('powershell.exe', refused('EACCES'))
    })

    expect(isBlockedSpawn(wrapped)).toBe(true)
    expect(isBlockedSpawn(new Error('没有找到可用的 Git Bash。'))).toBe(false)
    expect(isBlockedSpawn(undefined)).toBe(false)
  })

  it('stops walking instead of looping on a self-referencing cause', () => {
    const loop: Error & { cause?: unknown } = new Error('loop')
    loop.cause = loop

    expect(isBlockedSpawn(loop)).toBe(false)
  })
})

it('advice lists the directories to trust and where to add them', () => {
  const advice = securityBlockAdvice(['C:\\Programs\\App', 'C:\\Users\\l\\AppData\\Roaming\\App', ''])

  expect(advice).toContain('    C:\\Programs\\App')
  expect(advice).toContain('    C:\\Users\\l\\AppData\\Roaming\\App')
  expect(advice).toContain('火绒')
  expect(advice).toContain('360')
  // An empty path would render as a blank bullet the user cannot act on.
  expect(advice).not.toMatch(/\n {4}$/m)
})

describe('collapseWindowsPath', () => {
  const env = {
    LOCALAPPDATA: 'C:\\Users\\developer\\AppData\\Local',
    APPDATA: 'C:\\Users\\developer\\AppData\\Roaming',
    ProgramFiles: 'C:\\Program Files',
    USERPROFILE: 'C:\\Users\\developer'
  }

  it('hides the account name behind the variable that owns the path', () => {
    expect(collapseWindowsPath('C:\\Users\\developer\\AppData\\Roaming\\LLANO Agent', env)).toBe(
      '%APPDATA%\\LLANO Agent'
    )
    // %LOCALAPPDATA% lives inside %USERPROFILE%; the longer match has to win.
    expect(collapseWindowsPath('C:\\Users\\developer\\AppData\\Local\\Programs\\Git', env)).toBe(
      '%LOCALAPPDATA%\\Programs\\Git'
    )
    expect(collapseWindowsPath('C:\\Users\\developer\\Desktop', env)).toBe('%USERPROFILE%\\Desktop')
  })

  it('leaves a path that carries no account name alone', () => {
    // A chosen install directory (the installer allows one) has nothing to hide.
    expect(collapseWindowsPath('D:\\Apps\\LLANO Agent', env)).toBe('D:\\Apps\\LLANO Agent')
    expect(collapseWindowsPath('', env)).toBe('')
    expect(collapseWindowsPath('C:\\Users\\developer', env)).toBe('%USERPROFILE%')
    // A different case on the same directory is still the same directory.
    expect(collapseWindowsPath('c:\\users\\developer\\appdata\\roaming\\x', env)).toBe('%APPDATA%\\x')
  })
})
