import { describe, expect, it } from 'vitest'

import { blockedDirectories, runtimeBlockNotice } from './boot-failure-security'

// What main.ts composes when a required component was refused: the per-component
// detail first, then the advice block.
const BLOCKED = [
  'Git Bash：Error: 终端环境准备失败：无法启动 C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe：被安全软件或组策略拦截（EPERM）',
  'Python：Error: 无法启动 C:\\Windows\\System32\\tar.exe：被安全软件或组策略拦截（EPERM）',
  '',
  '环境准备被安全软件或组策略拦截（程序无法启动子进程）。',
  '请把下列目录加入杀毒软件的信任区/白名单，然后重新打开应用：',
  '    %LOCALAPPDATA%\\Programs\\LLANO Agent',
  '    %APPDATA%\\LLANO Agent',
  '    %LOCALAPPDATA%\\Programs\\Git',
  '火绒：主界面 → 设置 → 信任区 → 添加目录（含子目录）。',
  '360：防护中心 → 设置 → 信任区 → 添加目录；拦截记录里也可直接选「恢复并信任」。',
  '全公司统一处理可由 IT 在杀毒软件控制中心下发信任区策略。'
].join('\n')

describe('runtimeBlockNotice', () => {
  it('keeps the instructions and drops the diagnostics above them', () => {
    const notice = runtimeBlockNotice(BLOCKED)!

    expect(notice.startsWith('环境准备被安全软件或组策略拦截')).toBe(true)
    expect(notice).toContain('火绒')
    // The per-component spawn lines stay off the card.
    expect(notice).not.toContain('powershell.exe')
    expect(notice).not.toContain('Git Bash：')
  })

  it('ignores a failure that is not an interference block', () => {
    expect(runtimeBlockNotice('Could not connect to Hermes gateway')).toBeNull()
    expect(runtimeBlockNotice('Python：Error: 无法启动 C:\\python\\python.exe：ENOENT')).toBeNull()
    expect(runtimeBlockNotice(null)).toBeNull()
    expect(runtimeBlockNotice(undefined)).toBeNull()
  })
})

it('lists the directories to trust, without the surrounding prose', () => {
  expect(blockedDirectories(runtimeBlockNotice(BLOCKED)!)).toEqual([
    '%LOCALAPPDATA%\\Programs\\LLANO Agent',
    '%APPDATA%\\LLANO Agent',
    '%LOCALAPPDATA%\\Programs\\Git'
  ])
})
