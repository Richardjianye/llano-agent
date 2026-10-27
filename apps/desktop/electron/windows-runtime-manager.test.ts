import { execFile } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { extractRuntimeArchive, fetchRuntime, overallRuntimePercent } from './windows-runtime-manager'
it('uses verified fallback bytes, reuses cache offline, and rejects tampered downloads', async () => {
  const bytes = Buffer.from('trusted runtime fixture')
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runtime-download-'))
  const requests: string[] = []

  const server = http.createServer((req, res) => {
    requests.push(req.url!)

    if (req.url === '/missing') {
      res.writeHead(503)
      res.end()

      return
    }

    res.end(req.url === '/tamper' ? Buffer.alloc(bytes.length, 0) : bytes)
  })

  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`

  const component = {
    id: 'git',
    label: 'Git',
    version: '1',
    sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    size: bytes.length,
    urls: [origin + '/missing', origin + '/good'],
    prefix: '',
    pathDirs: [],
    probe: '',
    probeArgs: [],
    required: true
  }

  try {
    const file = await fetchRuntime(component, dir, () => {})
    expect(file).toMatch(/\.exe$/)
    expect(fs.readFileSync(file)).toEqual(bytes)
    expect(requests).toEqual(['/missing', '/good'])
    expect(
      await fetchRuntime(
        component,
        dir,
        () => {},
        () => {
          throw new Error('offline')
        }
      )
    ).toBe(file)
    fs.rmSync(file)
    await expect(fetchRuntime({ ...component, urls: [origin + '/tamper'] }, dir, () => {})).rejects.toThrow('SHA-256')
    expect(fs.existsSync(file)).toBe(false)
    expect(fs.readdirSync(path.dirname(file))).not.toContain('llano-0.0.0-win32-x64.exe.part')
  } finally {
    await new Promise<void>(resolve => server.close(() => resolve()))
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

it('reports verified download progress without inventing completion', async () => {
  const bytes = Buffer.alloc(128 * 1024, 7)
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runtime-progress-'))
  const reports: { activity?: string; message: string; percent?: number }[] = []
  const server = http.createServer((_req, res) => res.end(bytes))

  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`

  try {
    await fetchRuntime(
      {
        id: 'python',
        label: 'Python',
        version: '1',
        sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
        size: bytes.length,
        urls: [origin + '/python.zip'],
        prefix: '',
        pathDirs: [],
        probe: 'python.exe',
        probeArgs: [],
        required: true
      },
      dir,
      (message, percent, activity) => reports.push({ activity, message, percent })
    )

    expect(reports[0]).toMatchObject({ percent: 0 })
    expect(reports.at(-1)).toMatchObject({
      activity: 'preparing',
      message: '正在下载 Python：100%',
      percent: 100
    })
    expect(reports.every(report => report.activity === 'preparing')).toBe(true)
    expect(
      reports.every(report => report.percent === undefined || (report.percent >= 0 && report.percent <= 100))
    ).toBe(true)
  } finally {
    await new Promise<void>(resolve => server.close(() => resolve()))
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

describe('overallRuntimePercent', () => {
  // Real manifest magnitudes (MB): Git 65, ripgrep 1.8, browser engine 205
  const sizes = [65, 1.8, 205]

  it('weights stages by download size, not by count', () => {
    // Finishing Git + ripgrep is a quarter of the bytes (66.8/271.8), not two
    // thirds as equal stage weights claimed.
    expect(overallRuntimePercent(sizes, 2, 0)).toBe(25)
    // Half of the browser engine lands at (66.8 + 102.5)/271.8 — a big step,
    // where equal weights moved the bar by one sixth.
    expect(overallRuntimePercent(sizes, 2, 50)).toBe(62)
  })

  it('clamps stage percent and ends at exactly 100', () => {
    expect(overallRuntimePercent(sizes, 0, -5)).toBe(0)
    expect(overallRuntimePercent(sizes, 2, 100)).toBe(100)
    expect(overallRuntimePercent(sizes, 2, 250)).toBe(100)
  })

  it('falls back to equal stages when sizes are missing', () => {
    expect(overallRuntimePercent([0, 0, 0, 0], 1, 50)).toBe(38)
    expect(overallRuntimePercent([], 0, 50)).toBe(0)
  })
})

it('download progress carries the component, its step number and its own percentage', async () => {
  // 2026-09-20 首启实测反馈：整段只显示一句「正在准备环境」，卡住时截图看不出
  // 是哪个组件、到哪一步了。结构化字段让渲染层能说清楚，又不带镜像主机名。
  const bytes = Buffer.alloc(64 * 1024, 3)
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runtime-step-'))
  const steps: unknown[] = []
  const server = http.createServer((_req, res) => res.end(bytes))

  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`

  try {
    await fetchRuntime(
      {
        id: 'python',
        label: 'Python',
        version: '1',
        sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
        size: bytes.length,
        urls: [origin + '/python.zip'],
        prefix: '',
        pathDirs: [],
        probe: 'python.exe',
        probeArgs: [],
        required: true
      },
      dir,
      (_message, percent) => steps.push(percent)
    )

    expect(steps.at(-1)).toBe(100)
    expect(steps.every(percent => percent === undefined || (Number(percent) >= 0 && Number(percent) <= 100))).toBe(true)
  } finally {
    await new Promise<void>(resolve => server.close(() => resolve()))
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

describe('extractRuntimeArchive', () => {
  // Windows ships bsdtar as System32\tar.exe; macOS ships the same libarchive
  // build, so the real argv — including reading a .zip — is exercised here and
  // not only mocked. Why bsdtar at all: PowerShell is what antivirus refuses
  // first, and on 2026-09-20 a refused powershell.exe took down every component
  // that needed unpacking (0.18.2 field report).
  // bsdtar both reads and writes zip, so the fixture needs no extra tool and the
  // suite runs on Windows too (where there is no /usr/bin/zip).
  const TAR =
    process.platform === 'win32'
      ? path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'tar.exe')
      : '/usr/bin/tar'

  const exec = promisify(execFile)
  const simulatedDirs: string[] = []
  afterEach(() => {
    for (const dir of simulatedDirs.splice(0)) {fs.rmSync(dir, { recursive: true, force: true })}
    vi.restoreAllMocks()
  })

  function simulatedArchive() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runtime-中文 with space-'))
    simulatedDirs.push(dir)
    const archive = path.join(dir, '归档.zip')
    fs.writeFileSync(archive, 'verified cached fixture')

    return { archive, destination: path.join(dir, 'runtime', 'node-0000') }
  }

  function fixture(prefix: string) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runtime-中文 with space-'))
    const source = path.join(dir, 'source', prefix)

    fs.mkdirSync(path.join(source, 'doc'), { recursive: true })
    fs.writeFileSync(path.join(source, 'rg.exe'), 'binary')
    fs.writeFileSync(path.join(source, 'doc', 'README'), 'docs')
    // A sibling outside the manifest's prefix must not reach the runtime.
    fs.writeFileSync(path.join(dir, 'source', 'unrelated.txt'), 'skip me')

    return { dir, destination: path.join(dir, 'runtime', 'ripgrep-0000') }
  }

  function expectUnpacked(destination: string) {
    expect(fs.readFileSync(path.join(destination, 'rg.exe'), 'utf8')).toBe('binary')
    expect(fs.readFileSync(path.join(destination, 'doc', 'README'), 'utf8')).toBe('docs')
    expect(fs.existsSync(path.join(destination, 'unrelated.txt'))).toBe(false)
    expect(fs.existsSync(`${destination}.unpack`)).toBe(false)
  }

  it('unpacks a zip and keeps only the manifest prefix', async () => {
    const { dir, destination } = fixture('ripgrep-15.1.0-x86_64-pc-windows-msvc')
    const archive = path.join(dir, '已验证 缓存.zip')

    try {
      await exec(TAR, ['-c', '-f', archive, '--format', 'zip', '-C', path.join(dir, 'source'), '.'])
      expect(
        await extractRuntimeArchive({
          archive,
          destination,
          resources: dir,
          prefix: 'ripgrep-15.1.0-x86_64-pc-windows-msvc/',
          tar: TAR,
          runner: (file, args, options) => {
            expect(args.every(arg => Array.from(arg).every(char => char.charCodeAt(0) < 128))).toBe(true)
            expect(options?.cwd).toContain('中文 with space')

            return exec(file, args, options)
          }
        })
      ).toBe('tar')
      expectUnpacked(destination)
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })

  it('unpacks a tarball the same way (the Python runtime ships as .tar.gz)', async () => {
    const { dir, destination } = fixture('python')
    const archive = path.join(dir, '已验证 Python.tar.gz')

    try {
      await exec(TAR, ['-czf', archive, '-C', path.join(dir, 'source'), '.'])
      expect(
        await extractRuntimeArchive({
          archive,
          destination,
          resources: dir,
          prefix: 'python/',
          format: 'tar',
          tar: TAR,
          runner: (file, args, options) => {
            expect(args.every(arg => Array.from(arg).every(char => char.charCodeAt(0) < 128))).toBe(true)
            expect(options?.cwd).toContain('中文 with space')

            return exec(file, args, options)
          }
        })
      ).toBe('tar')
      expectUnpacked(destination)
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })

  it('refuses a traversing entry without retrying it through PowerShell', async () => {
    const calls: string[] = []
    const { archive, destination } = simulatedArchive()

    await expect(
      extractRuntimeArchive({
        archive,
        destination,
        resources: 'C:\\app\\resources',
        prefix: 'node/',
        runner: (file, args) => {
          calls.push(file)

          return Promise.resolve({ stdout: 'node/bin/node.exe\n../../evil.exe\n', stderr: '' })
        }
      })
    ).rejects.toThrow('Unsafe runtime archive entry')
    expect(calls.some(file => /powershell/i.test(file))).toBe(false)
  })

  it('falls back to the PowerShell unpacker when bsdtar is unusable', async () => {
    const calls: { file: string; args: string[] }[] = []
    const { archive, destination } = simulatedArchive()

    expect(
      await extractRuntimeArchive({
        archive,
        destination,
        resources: 'C:\\app\\resources',
        prefix: 'node-v26.8.1-win-x64/',
        runner: (file, args) => {
          calls.push({ file, args })

          if (/tar\.exe$/i.test(file)) {
            // Windows Server 2016 and 1803 have no bsdtar at all.
            return Promise.reject(Object.assign(new Error('spawn ENOENT'), { code: 'ENOENT' }))
          }

          return Promise.resolve({ stdout: '', stderr: '' })
        }
      })
    ).toBe('powershell')

    const fallback = calls.at(-1)!
    expect(fallback.file).toMatch(/powershell\.exe$/i)
    expect(fallback.args).toContain('-NonInteractive')
    expect(fallback.args.join(' ')).toContain('extract.ps1')
    expect(fallback.args).toContain('node-v26.8.1-win-x64/')
  })

  it('blames bsdtar, not PowerShell, when both are refused', async () => {
    const { archive, destination } = simulatedArchive()
    const refusedTar = Object.assign(new Error('spawn EPERM'), { code: 'EPERM' })
    const refusedShell = Object.assign(new Error('spawn EPERM'), { code: 'EPERM' })

    // Whitelisting System32\tar.exe is what the normal path needs; naming the
    // fallback would send the user after the wrong binary.
    await expect(
      extractRuntimeArchive({
        archive,
        destination,
        resources: 'C:\\app\\resources',
        runner: file => Promise.reject(/tar\.exe$/i.test(file) ? refusedTar : refusedShell)
      })
    ).rejects.toBe(refusedTar)
  })

  it('copies the prefix into its final directory without renaming any directory', async () => {
    const { dir, destination } = fixture('python')
    const archive = path.join(dir, '缓存.tar.gz')

    try {
      await exec(TAR, ['-czf', archive, '-C', path.join(dir, 'source'), '.'])

      const rename = vi.spyOn(fs, 'renameSync').mockImplementation(() => {
        throw new Error('Directory move refused')
      })

      await extractRuntimeArchive({ archive, destination, resources: dir, prefix: 'python/', format: 'tar', tar: TAR })
      expectUnpacked(destination)
      expect(rename).not.toHaveBeenCalled()
      expect(fs.readdirSync(path.dirname(destination))).toEqual([path.basename(destination)])
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })

  it('records native failures immediately before fallback, with code/stderr/helper', async () => {
    const { archive, destination } = simulatedArchive()
    const sequence: string[] = []
    const diagnostics: unknown[] = []
    const error = Object.assign(new Error('Cannot open archive'), { code: 47, stderr: 'tar: failed to open archive' })
    await extractRuntimeArchive({
      archive,
      destination,
      resources: path.dirname(archive),
      runner: async file => {
        if (/tar\.exe$/i.test(file)) {throw error}
        sequence.push('fallback-run')
      },
      diagnostic: event => {
        diagnostics.push(event)
        sequence.push(event.phase + ':' + event.status)
      }
    })
    expect(sequence.indexOf('extract.tar.list:failed')).toBeLessThan(sequence.indexOf('fallback-run'))
    expect(diagnostics).toContainEqual(
      expect.objectContaining({
        phase: 'extract.tar.list',
        status: 'failed',
        code: 47,
        stderr: error.stderr,
        helper: expect.stringMatching(/tar\.exe$/i)
      })
    )
  })

  it('does not replace a helper failure when temporary cleanup also fails', async () => {
    const { archive, destination } = simulatedArchive()
    const original = Object.assign(new Error('native refused'), { code: 'EPERM', stderr: 'original stderr' })

    const cleanup = vi
      .spyOn(fs.promises, 'rm')
      .mockRejectedValue(Object.assign(new Error('cleanup denied'), { code: 'EACCES' }))

    await expect(
      extractRuntimeArchive({
        archive,
        destination,
        resources: path.dirname(archive),
        runner: async () => {
          throw original
        }
      })
    ).rejects.toBe(original)
    cleanup.mockRestore()
  })

  it('keeps a usable final directory when staging cleanup alone fails', async () => {
    const { dir, destination } = fixture('python')
    const archive = path.join(dir, '缓存.tar.gz')

    try {
      await exec(TAR, ['-czf', archive, '-C', path.join(dir, 'source'), '.'])

      const cleanup = vi
        .spyOn(fs.promises, 'rm')
        .mockRejectedValue(Object.assign(new Error('cleanup denied'), { code: 'EACCES' }))

      const events: unknown[] = []
      await expect(
        extractRuntimeArchive({
          archive,
          destination,
          resources: dir,
          prefix: 'python/',
          format: 'tar',
          tar: TAR,
          diagnostic: e => events.push(e)
        })
      ).resolves.toBe('tar')
      expectUnpacked(destination)
      expect(events).toContainEqual(
        expect.objectContaining({ phase: 'extract.cleanup', status: 'failed', code: 'EACCES' })
      )
      cleanup.mockRestore()
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })

  it('rejects unsafe manifest prefixes before spawning a helper', async () => {
    const { archive, destination } = simulatedArchive()
    const runner = vi.fn()
    await expect(
      extractRuntimeArchive({ archive, destination, resources: 'unused', prefix: '../outside', runner })
    ).rejects.toThrow('Unsafe runtime archive prefix')
    expect(runner).not.toHaveBeenCalled()
  })
})
