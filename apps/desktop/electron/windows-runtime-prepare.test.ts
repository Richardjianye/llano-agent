import type * as ChildProcess from 'node:child_process'
import type { execFile as originalExecFile } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'

import { afterEach, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({ systemPython: '', git: '' }))
vi.mock('./windows-git-runtime', () => ({
  ensureWindowsGit: async () => ({ bash: state.git, root: path.dirname(state.git), installed: false }),
  gitRuntimeEnvironment: (_git: unknown, current: string) => ({ PATH: current })
}))
vi.mock('node:child_process', async importOriginal => {
  const actual = await importOriginal<typeof ChildProcess>()
  const realAsync = promisify(actual.execFile)

  const wrapped = Object.assign((...args: Parameters<typeof originalExecFile>) => actual.execFile(...args), {
    [promisify.custom]: async (file: string, args: string[], options: unknown) => {
      if (file.endsWith('where.exe')) {
        return { stdout: state.systemPython, stderr: '' }
      }

      return realAsync(file, args, options as Parameters<typeof realAsync>[2])
    }
  })

  return { ...actual, execFile: wrapped }
})

import { prepareWindowsRuntime, type RuntimeDiagnostic } from './windows-runtime-manager'

const dirs: string[] = []
afterEach(() => {
  state.systemPython = ''

  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), '运行环境 cached '))
  dirs.push(dir)
  state.git = path.join(dir, 'git', 'bash.exe')
  const resources = path.join(dir, 'resources')
  fs.mkdirSync(path.join(resources, 'windows-runtime'), { recursive: true })
  const home = path.join(dir, '用户 配置')

  const component = {
    id: 'python',
    label: 'Python',
    version: '3.13',
    sha256: crypto.createHash('sha256').update('archive').digest('hex'),
    size: 7,
    urls: ['https://runtime.invalid/python.tar.gz'],
    prefix: 'python/',
    format: 'tar',
    pathDirs: [''],
    probe: 'python.exe',
    probeArgs: ['--version'],
    required: true
  }

  fs.writeFileSync(
    path.join(resources, 'windows-runtime', 'manifest.json'),
    JSON.stringify({
      git: { ...component, id: 'git' },
      components: [component]
    })
  )

  return {
    dir,
    resources,
    home,
    component,
    base: path.join(home, 'runtime', `python-${component.sha256.slice(0, 16)}`)
  }
}

function executable(filename: string, text: string) {
  fs.mkdirSync(path.dirname(filename), { recursive: true })
  fs.writeFileSync(filename, `#!/bin/sh\nprintf '%s\\n' '${text}'\n`)
  fs.chmodSync(filename, 0o700)
}

// Native POSIX probes exercise the actual import/filesystem resolution chain.
// Real Windows executables/ACP extraction remain an explicit pre-release gate.
it.skipIf(process.platform === 'win32')(
  'reuses a validated UUID cache without download, extraction or moving its root',
  async () => {
    const { resources, home, component, base } = fixture()
    const root = base + '-existing-uuid'
    executable(path.join(root, component.probe), 'Python 3.13.3')
    fs.writeFileSync(base + '.json', JSON.stringify({ directory: path.basename(root) }))
    const fetcher = vi.fn(async () => new Response('', { status: 503 }))
    const events: RuntimeDiagnostic[] = []

    const result = await prepareWindowsRuntime(
      resources,
      home,
      true,
      () => {},
      fetcher,
      event => events.push(event)
    )

    expect(result.requiredReady).toBe(true)
    expect(result.items.find(item => item.id === 'python')?.detail).toBe(root)
    expect(fetcher.mock.calls).toHaveLength(1) // Connectivity HEAD only; no artifact GET.
    expect(fs.existsSync(root)).toBe(true)
    expect(events.some(event => event.phase.startsWith('extract'))).toBe(false)
  }
)

it.skipIf(process.platform === 'win32')(
  'falls back to compatible system Python before downloading a missing managed runtime',
  async () => {
    const { dir, resources, home } = fixture()
    const system = path.join(dir, '系统 Python', 'python.exe')
    executable(system, 'Python 3.13.3')
    state.systemPython = system + '\n'
    const fetcher = vi.fn(async () => new Response('', { status: 503 }))
    const events: RuntimeDiagnostic[] = []

    const result = await prepareWindowsRuntime(
      resources,
      home,
      true,
      () => {},
      fetcher,
      event => events.push(event)
    )

    expect(result.requiredReady).toBe(true)
    expect(result.items.find(item => item.id === 'python')?.detail).toBe(path.dirname(system))
    expect(fetcher.mock.calls).toHaveLength(1)
    expect(events).toContainEqual(
      expect.objectContaining({ phase: 'probe.cached', status: 'failed', component: 'python', code: 'ENOENT' })
    )
  }
)

it('logs a required component failure before reporting that stage failed and preserves its native code/helper', async () => {
  const { resources, home } = fixture()
  const sequence: string[] = []
  const events: RuntimeDiagnostic[] = []

  const result = await prepareWindowsRuntime(
    resources,
    home,
    false,
    (_message, _percent, _activity, step) => {
      if (step?.kind === 'failed') {
        sequence.push('ui-failed')
      }
    },
    async () => new Response('', { status: 503 }),
    event => {
      events.push(event)
      sequence.push(event.phase + ':' + event.status)
    }
  )

  expect(result.requiredReady).toBe(false)
  expect(sequence.indexOf('probe.cached:failed')).toBeLessThan(sequence.indexOf('ui-failed'))
  expect(events).toContainEqual(
    expect.objectContaining({
      component: 'python',
      phase: 'probe.cached',
      status: 'failed',
      code: 'ENOENT',
      helper: expect.stringContaining('python.exe')
    })
  )
})
