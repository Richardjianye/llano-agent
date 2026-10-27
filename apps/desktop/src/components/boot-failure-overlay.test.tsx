import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { $desktopOnboarding } from '@/store/onboarding'
import { $productAuthenticated } from '@/store/product-auth'

import { BootFailureOverlay } from './boot-failure-overlay'

// Remote-backend users hit a hard boot failure that isn't OAuth reauth (token
// auth, wrong URL, unreachable host). The recovery screen must let them fix the
// remote connection in place — the "Connection settings" action swaps the card
// to an in-line connect form — instead of stranding them (the old bug forced a
// hand-edit of connection.json).

function failBoot(error = 'Could not connect to Hermes gateway') {
  $desktopBoot.set({
    error,
    fakeMode: false,
    message: 'boot failed',
    phase: 'renderer.error',
    progress: 40,
    running: false,
    timestamp: Date.now(),
    visible: true
  })
}

function stubDesktop(config: Record<string, unknown>) {
  const original = window.hermesDesktop
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { getRecentLogs: async () => ({ lines: [] }), getConnectionConfig: async () => config }
  })

  return () => Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
}

const remoteToken = {
  envOverride: false,
  mode: 'remote',
  profile: null,
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: true,
  remoteUrl: 'http://100.116.104.53:9191',
  cloudOrg: ''
}

beforeEach(() => {
  $productAuthenticated.set(true)
  $desktopOnboarding.set({
    configured: true,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  })
  failBoot()
})

afterEach(cleanup)

describe('BootFailureOverlay', () => {
  it('stays hidden when a branded product session has been invalidated', () => {
    $productAuthenticated.set(false)
    render(<BootFailureOverlay />)

    expect(screen.queryByText(/Hermes failed to start/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('keeps the company gateway address locked on a local failure', () => {
    render(<BootFailureOverlay />)

    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /gateway settings/i })).toBeNull()
  })

  it('keeps raw local paths off the branded failure card', () => {
    const diagnostic = 'spawn C:\\Users\\alice\\private\\python.exe ENOENT via 192.0.2.130'
    failBoot(diagnostic)
    render(<BootFailureOverlay />)

    expect(screen.getByText(/Technical details are available in the desktop logs/)).toBeTruthy()
    expect(screen.queryByText(diagnostic)).toBeNull()
    expect(screen.queryByText(/C:\\Users\\alice|192\.168\.25\.130/)).toBeNull()
  })

  it('puts the antivirus trust-list directories on the branded card', () => {
    // 2026-09-20：0.18.2 装机被 360 静默拦截，用户只看到一句「启动未完成」。
    // 加白是唯一解，所以这条指引必须出现在这张卡上，而不是只躺在日志里。
    failBoot(
      [
        'Git Bash：Error: 无法启动 C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe：被安全软件或组策略拦截（EPERM）',
        '',
        '环境准备被安全软件或组策略拦截（程序无法启动子进程）。',
        '请把下列目录加入杀毒软件的信任区/白名单，然后重新打开应用：',
        '    %APPDATA%\\LLANO Agent',
        '火绒：主界面 → 设置 → 信任区 → 添加目录（含子目录）。'
      ].join('\n')
    )
    render(<BootFailureOverlay />)

    expect(screen.getByText(/%APPDATA%\\LLANO Agent/)).toBeTruthy()
    expect(screen.getByText(/火绒/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /copy/i })).toBeTruthy()
    // Still no diagnostics and no expanded local path on a card people forward.
    expect(screen.queryByText(/Technical details are available in the desktop logs/)).toBeNull()
    expect(screen.queryByText(/powershell\.exe/)).toBeNull()
    // Whitelisting then Retry is the fix, so that button has to stay.
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
  })

  it('drops local-only Repair and Use-local-gateway on a local failure', () => {
    render(<BootFailureOverlay />)
    // No connection config stub → treated as a local failure.
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /restart backend/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /use local gateway/i })).toBeNull()
  })

  it('leads with Gateway settings and drops Repair for a remote (token) failure', async () => {
    const restore = stubDesktop(remoteToken)

    try {
      render(<BootFailureOverlay />)
      await waitFor(() => expect(screen.queryByRole('button', { name: /repair/i })).toBeNull())
      expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /use local gateway/i })).toBeTruthy()
    } finally {
      restore()
    }
  })
})
