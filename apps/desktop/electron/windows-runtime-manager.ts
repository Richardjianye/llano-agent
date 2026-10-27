import { execFile } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

import { downloadAndVerifyArtifact } from './update-download'
import { ensureWindowsGit, gitRuntimeEnvironment } from './windows-git-runtime'
import { decorateSpawnFailure, isBlockedSpawn } from './windows-spawn-failure'
const exec = promisify(execFile)

function system32(name: string) {
  return path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', name)
}

/** Every helper process goes through here so a refusal names the executable. */
async function run(file: string, args: string[], options: { timeout?: number; cwd?: string } = {}) {
  try {
    return await exec(file, args, { windowsHide: true, ...options })
  } catch (error) {
    throw decorateSpawnFailure(file, error)
  }
}

interface RuntimeComponent {
  id: string
  label: string
  version: string
  sha256: string
  size: number
  urls: string[]
  prefix: string
  format?: 'zip' | 'tar' | 'file'
  pathDirs: string[]
  probe: string
  probeArgs: string[]
  required: boolean
  browser?: string
}

export type RuntimeActivity = 'checking' | 'preparing'

/**
 * What the user is told while a first launch prepares the runtime.
 *
 * Structured on purpose: the raw messages carry a mirror hostname, and other
 * boot phases carry local paths — neither belongs on a screen someone will
 * screenshot. The renderer builds its own localized line from these fields,
 * so a stuck first launch still says WHICH component and HOW FAR it got.
 */
export interface RuntimeStep {
  index: number
  total: number
  label: string
  kind: 'check' | 'download' | 'extract' | 'ready' | 'failed'
  percent?: number
}

type RuntimeProgress = (message: string, percent?: number, activity?: RuntimeActivity, step?: RuntimeStep) => void

export interface RuntimeHealth {
  supported: boolean
  ok: boolean
  requiredReady: boolean
  intranet: boolean | null
  // `blocked` marks a component that antivirus/policy refused to *start*, which
  // is fixed by whitelisting rather than by repairing the install.
  items: { id: string; label: string; ok: boolean; detail: string; blocked?: boolean }[]
  env: Record<string, string>
}

function prependPath(entries: string[], current: string) {
  const seen = new Set<string>()

  return [...entries, ...current.split(';')]
    .filter(entry => {
      const key = entry.toLowerCase()

      if (!entry || seen.has(key)) {
        return false
      }

      seen.add(key)

      return true
    })
    .join(';')
}

async function hash(file: string) {
  const digest = crypto.createHash('sha256')

  for await (const chunk of fs.createReadStream(file)) {
    digest.update(chunk)
  }

  return digest.digest('hex')
}

/** Downloads only fixed bytes described by the manifest inside the signed app. */
export async function fetchRuntime(
  component: RuntimeComponent,
  cache: string,
  progress: RuntimeProgress,
  fetchImpl = fetch
) {
  const stagingDir = path.join(cache, component.sha256)
  const archive = path.join(stagingDir, component.id === 'git' ? 'payload.exe' : 'payload.zip')

  if (
    fs.existsSync(archive) &&
    fs.statSync(archive).size === component.size &&
    (await hash(archive)) === component.sha256
  ) {
    return archive
  }

  fs.rmSync(archive, { force: true })
  const errors: string[] = []

  for (const url of component.urls) {
    progress(`正在下载 ${component.label}（${new URL(url).hostname}）…`, 0, 'preparing')

    try {
      let lastPercent = -1

      const file = await downloadAndVerifyArtifact({
        stagingDir,
        gatewayOrigin: new URL(url).origin,
        // The application manifest is the trust root here; these are not update offers.
        manifest: {
          schema: 1,
          version: '0.0.0',
          platform: 'win32',
          arch: 'x64',
          policy: 'optional',
          min_supported_version: '0.0.0',
          released_at: '2026-09-09T00:00:00Z',
          release_notes: '',
          artifact_url: url,
          artifact_sha256: component.sha256,
          artifact_size: component.size
        },
        fetchImpl: async (input, options) => {
          const headers = new AbortController()
          const timer = setTimeout(() => headers.abort(), 8_000)

          try {
            return await fetchImpl(input, {
              ...options,
              signal: AbortSignal.any([headers.signal, options!.signal!, AbortSignal.timeout(15 * 60_000)])
            })
          } finally {
            clearTimeout(timer)
          }
        },
        onProgress: (received, total) => {
          const percent = Math.floor((received / total) * 100)

          if (percent !== lastPercent) {
            lastPercent = percent
            progress(`正在下载 ${component.label}：${percent}%`, percent, 'preparing')
          }
        }
      })

      fs.renameSync(file, archive)

      return archive
    } catch (error) {
      errors.push(`${new URL(url).hostname}: ${String(error)}`)
    }
  }

  throw new Error(`${component.label} 下载失败。请连接公司内网后重试。${errors.join('；')}`)
}

async function probe(component: RuntimeComponent, root: string, env: NodeJS.ProcessEnv) {
  const temporary = fs.mkdtempSync(path.join(process.env.TEMP || process.cwd(), 'llano-runtime-probe-'))
  const executable = path.join(root, component.probe)

  try {
    await new Promise<void>((resolve, reject) => {
      let timer: NodeJS.Timeout | undefined
      let child: ReturnType<typeof execFile>

      try {
        child = execFile(
          executable,
          component.probeArgs.map(arg => arg.replace('{temp}', temporary)),
          { env, windowsHide: true, maxBuffer: 1024 * 1024 },
          error => {
            clearTimeout(timer)

            if (error) {
              reject(decorateSpawnFailure(executable, error))
            } else {
              resolve()
            }
          }
        )
      } catch (error) {
        // Windows refuses a process by throwing here, before the callback exists.
        reject(decorateSpawnFailure(executable, error))

        return
      }

      timer = setTimeout(() => {
        // Kill only this probe's tree before its parent exits, so Chromium children
        // cannot outlive a failed check or keep the temporary profile locked.
        if (process.platform === 'win32' && child.pid) {
          void exec(system32('taskkill.exe'), ['/PID', String(child.pid), '/T', '/F'], {
            windowsHide: true,
            timeout: 5_000
          })
            .catch(() => child.kill())
            .finally(() => reject(new Error(`${component.label} 检查超时`)))
        } else {
          child.kill()
          reject(new Error(`${component.label} 检查超时`))
        }
      }, 20_000)
    })
  } finally {
    try {
      await fs.promises.rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
    } catch {
      /* An antivirus scan may briefly hold a profile file after the probe exits. */
    }
  }
}

async function findSystemBrowser(component: RuntimeComponent, env: NodeJS.ProcessEnv) {
  const roots = [process.env.ProgramFiles, process.env['ProgramFiles(x86)'], process.env.LOCALAPPDATA]

  for (const root of roots.filter((value): value is string => Boolean(value))) {
    for (const relative of ['Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe']) {
      const candidate = path.join(root, relative)

      if (!fs.existsSync(candidate)) {
        continue
      }

      try {
        // Use a disposable profile; never read or modify the user's browser profile.
        await probe({ ...component, probe: path.basename(candidate) }, path.dirname(candidate), env)

        return candidate
      } catch {
        /* A managed browser remains available when the installed one cannot launch. */
      }
    }
  }

  return null
}

async function findSystemRuntime(component: RuntimeComponent) {
  const commands: Record<string, { name: string; minimum: number; maximum?: number }> = {
    python: { name: 'python.exe', minimum: 3, maximum: 3 },
    node: { name: 'node.exe', minimum: 26 },
    uv: { name: 'uv.exe', minimum: 0 },
    ripgrep: { name: 'rg.exe', minimum: 14 },
    ffmpeg: { name: 'ffmpeg.exe', minimum: 6 }
  }

  const spec = commands[component.id]

  if (!spec) {
    return null
  }

  try {
    const { stdout } = await run(system32('where.exe'), [spec.name], { timeout: 5_000 })

    for (const candidate of stdout.trim().split(/\r?\n/)) {
      if (/\\WindowsApps\\/i.test(candidate)) {
        continue
      }

      try {
        const result = await exec(candidate, component.id === 'ffmpeg' ? ['-version'] : ['--version'], {
          windowsHide: true,
          timeout: 10_000
        })

        const version = /(?:^|\s|v)(\d+)\.(\d+)/.exec(result.stdout + result.stderr)

        if (!version || +version[1] < spec.minimum || (spec.maximum && +version[1] > spec.maximum)) {
          continue
        }

        if (component.id === 'python' && (+version[2] < 11 || +version[2] >= 14)) {
          continue
        }

        if (component.id === 'uv' && +version[1] === 0 && +version[2] < 8) {
          continue
        }

        const dir = path.dirname(candidate)

        if (component.id === 'node' && !fs.existsSync(path.join(dir, 'npm.cmd'))) {
          continue
        }

        if (component.id === 'uv' && !fs.existsSync(path.join(dir, 'uvx.exe'))) {
          continue
        }

        return dir
      } catch {
        /* Try the next installed candidate. */
      }
    }
  } catch {
    /* Missing system command; use the managed runtime. */
  }

  return null
}

/**
 * Whole-runtime progress weighted by each component's download size.
 *
 * Nearly all first-run time is spent downloading. With equal stage weights the
 * 1.8 MB ripgrep and the 205 MB browser engine each moved the bar by the same
 * ~11%, so it raced through small items and then sat still on the big one.
 * Falls back to equal weights if the manifest carries no sizes.
 */
export function overallRuntimePercent(sizes: readonly number[], index: number, stagePercent: number): number {
  const weights = sizes.map(size => (Number.isFinite(size) && size > 0 ? size : 0))
  const total = weights.reduce((sum, weight) => sum + weight, 0)
  const stage = Math.max(0, Math.min(100, stagePercent)) / 100

  if (!total) {
    return sizes.length ? Math.round(((index + stage) / sizes.length) * 100) : 0
  }

  const done = weights.slice(0, Math.max(0, index)).reduce((sum, weight) => sum + weight, 0)

  return Math.min(100, Math.round(((done + (weights[index] ?? 0) * stage) / total) * 100))
}

export interface RuntimeDiagnostic {
  component?: string
  phase: string
  status: 'start' | 'ready' | 'failed' | 'fallback'
  helper?: string
  code?: string | number
  message?: string
  stderr?: string
  archive?: string
  destination?: string
}

export type RuntimeDiagnosticReporter = (event: RuntimeDiagnostic) => void

function diagnose(report: RuntimeDiagnosticReporter | undefined, event: RuntimeDiagnostic, error?: unknown) {
  if (!report) {return}

  const detail = error as
    { code?: string | number; message?: string; stderr?: unknown; spawnFile?: string; cause?: unknown } | undefined

  const cause = detail?.cause as typeof detail

  try {
    report({
      ...event,
      ...(error === undefined
        ? {}
        : {
            code: detail?.code ?? cause?.code,
            message: detail?.message ?? String(error),
            stderr: String(detail?.stderr ?? cause?.stderr ?? '').slice(0, 8192),
            helper: event.helper ?? detail?.spawnFile ?? cause?.spawnFile
          })
    })
  } catch {
    /* Logging must not change installation or hide its original error. */
  }
}

type ArchiveRunner = (file: string, args: string[], options?: { timeout?: number; cwd?: string }) => Promise<unknown>

interface ExtractRequest {
  archive: string
  destination: string
  resources: string
  prefix?: string
  format?: 'zip' | 'tar'
  /** Overridable so the unpacker can be exercised against a real bsdtar in tests. */
  tar?: string
  runner?: ArchiveRunner
  diagnostic?: RuntimeDiagnosticReporter
}

function unsafeArchive(reason: string) {
  return Object.assign(new Error(reason), { unsafeArchive: true })
}

function checkedPrefix(prefix: string) {
  const normalized = prefix.replace(/\\/g, '/')

  if (normalized.startsWith('/') || normalized.includes(':') || normalized.split('/').includes('..')) {
    throw unsafeArchive('Unsafe runtime archive prefix')
  }

  return normalized.split('/').filter(Boolean)
}

async function extractWithTar(
  tar: string,
  { destination, prefix = '', diagnostic }: ExtractRequest,
  workspace: string,
  archiveName: string,
  runner: ArchiveRunner
) {
  diagnose(diagnostic, { phase: 'extract.tar.list', status: 'start', helper: tar })
  let listing: { stdout?: string }

  try {
    listing = (await runner(tar, ['-tf', archiveName], { timeout: 120_000, cwd: workspace })) as { stdout?: string }
  } catch (error) {
    diagnose(diagnostic, { phase: 'extract.tar.list', status: 'failed', helper: tar }, error)
    throw error
  }

  const entries = String(listing?.stdout ?? '')
    .split(/\r?\n/)
    .map(entry => entry.trim())
    .filter(Boolean)

  if (!entries.length) {throw new Error('Cannot inspect runtime archive')}

  for (const entry of entries) {
    const name = entry.replace(/\\/g, '/')

    if (name.startsWith('/') || name.includes(':') || name.split('/').includes('..')) {
      throw unsafeArchive('Unsafe runtime archive entry')
    }
  }

  const content = path.join(workspace, 'content')
  fs.mkdirSync(content, { recursive: true })
  diagnose(diagnostic, { phase: 'extract.tar.unpack', status: 'start', helper: tar })

  try {
    // Native tar only sees ASCII relative paths. Node's cwd and filesystem APIs
    // carry the user's Unicode directory without a narrow argv conversion.
    await runner(tar, ['-xf', archiveName, '-C', 'content'], { timeout: 300_000, cwd: workspace })
  } catch (error) {
    diagnose(diagnostic, { phase: 'extract.tar.unpack', status: 'failed', helper: tar }, error)
    throw error
  }

  const source = path.join(content, ...checkedPrefix(prefix))

  if (!fs.existsSync(source) || !fs.lstatSync(source).isDirectory()) {
    throw unsafeArchive('Runtime archive is missing its expected directory')
  }

  diagnose(diagnostic, { phase: 'extract.copy', status: 'start', destination })
  // Keep the final UUID directory in place, including after its executable probe.
  // Copy only the signed manifest prefix; never move a directory that DLLs or
  // another process may have opened.
  fs.cpSync(source, destination, { recursive: true, force: false, errorOnExist: true })
  diagnose(diagnostic, { phase: 'extract.copy', status: 'ready', destination })
}

/**
 * Unpacks a verified runtime archive with `System32\tar.exe` — the bsdtar that
 * ships with Windows since 1809, and which this manifest already relies on for
 * .tar.gz — falling back to the bundled PowerShell unpacker.
 *
 * PowerShell is the first thing corporate antivirus refuses to let an unknown
 * app start. On 2026-09-20 a user's machine refused it (360) and every component
 * that needed it died with a nameless `spawn EPERM`, while the two that never
 * start an interpreter came up fine. Keeping the normal path free of a script
 * host removes that whole class of failure; the fallback stays for a Windows
 * without bsdtar or an archive it cannot read.
 */
export async function extractRuntimeArchive(request: ExtractRequest): Promise<'tar' | 'powershell'> {
  const { archive, destination, resources, prefix = '', format = 'zip', diagnostic } = request
  const runner = request.runner ?? run
  checkedPrefix(prefix)

  if (fs.existsSync(destination)) {throw new Error('Runtime destination already exists')}
  fs.mkdirSync(path.dirname(destination), { recursive: true })
  const workspace = fs.mkdtempSync(path.join(path.dirname(destination), 'extract-'))
  const archiveName = format === 'tar' ? 'payload.tar' : 'payload.zip'

  try {
    // Copy from the verified cache with Node, without exposing an absolute
    // Unicode archive name to either native tar invocation.
    fs.copyFileSync(archive, path.join(workspace, archiveName))

    try {
      await extractWithTar(request.tar ?? system32('tar.exe'), request, workspace, archiveName, runner)

      return 'tar'
    } catch (error) {
      diagnose(diagnostic, { phase: 'extract.tar', status: 'failed' }, error)

      if ((error as { unsafeArchive?: boolean }).unsafeArchive) {throw error}
      const shell = system32(path.join('WindowsPowerShell', 'v1.0', 'powershell.exe'))
      diagnose(diagnostic, { phase: 'extract.powershell', status: 'fallback', helper: shell })

      try {
        fs.rmSync(destination, { recursive: true, force: true })
        await runner(
          shell,
          [
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            path.join(resources, 'windows-runtime', 'extract.ps1'),
            '-Archive',
            archiveName,
            '-Destination',
            destination,
            '-Prefix',
            prefix,
            '-Format',
            format
          ],
          { timeout: 300_000, cwd: workspace }
        )

        return 'powershell'
      } catch (fallbackError) {
        diagnose(diagnostic, { phase: 'extract.powershell', status: 'failed', helper: shell }, fallbackError)
        throw isBlockedSpawn(error) ? error : fallbackError
      }
    }
  } finally {
    try {
      await fs.promises.rm(workspace, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
    } catch (error) {
      diagnose(diagnostic, { phase: 'extract.cleanup', status: 'failed', destination: workspace }, error)
    }
  }
}

export async function prepareWindowsRuntime(
  resources: string,
  home: string,
  repair: boolean,
  progress: RuntimeProgress = () => {},
  fetchImpl = fetch,
  diagnostic?: RuntimeDiagnosticReporter
): Promise<RuntimeHealth> {
  const manifest = JSON.parse(fs.readFileSync(path.join(resources, 'windows-runtime', 'manifest.json'), 'utf8')) as {
    git: RuntimeComponent
    components: RuntimeComponent[]
  }

  const result: RuntimeHealth = { supported: true, ok: true, requiredReady: true, intranet: null, items: [], env: {} }
  const stages = [manifest.git, ...manifest.components]
  const stageSizes = stages.map(component => component.size)

  const reportStage = (
    index: number,
    message: string,
    stagePercent = 0,
    activity: RuntimeActivity = 'checking',
    kind: RuntimeStep['kind'] = 'check',
    stagePercentForStep?: number
  ) => {
    const step: RuntimeStep = {
      index: index + 1,
      total: stages.length,
      label: stages[index]?.label ?? '',
      kind,
      ...(kind === 'download'
        ? { percent: Math.max(0, Math.min(100, Math.round(stagePercentForStep ?? stagePercent))) }
        : {})
    }

    progress(message, overallRuntimePercent(stageSizes, index, stagePercent), activity, step)
  }

  try {
    const response = await fetchImpl(manifest.git.urls[0], { method: 'HEAD', signal: AbortSignal.timeout(3_000) })
    result.intranet = response.ok
  } catch {
    result.intranet = false
  }

  if (!result.intranet) {
    for (const component of [manifest.git, ...manifest.components]) {
      const fallback = component.urls.filter(url => new URL(url).hostname !== '192.0.2.130')

      if (fallback.length) {
        component.urls = fallback
      }
    }
  }

  const runtime = path.join(home, 'runtime')
  const cache = path.join(runtime, 'downloads')
  let inheritedPath = process.env.PATH || process.env.Path || ''

  try {
    let git

    reportStage(0, '正在检查 Git Bash…', 0, 'checking', 'check')

    try {
      git = await ensureWindowsGit(resources, 'Check')
    } catch (error) {
      if (!repair) {
        throw error
      }

      const installer = await fetchRuntime(
        manifest.git,
        cache,
        (message, percent = 0) => reportStage(0, message, Math.round(percent * 0.85), 'preparing', 'download', percent),
        fetchImpl
      )

      reportStage(0, '正在静默安装 Git Bash…', 90, 'preparing', 'extract')
      git = await ensureWindowsGit(resources, 'Ensure', installer)
    }

    Object.assign(result.env, gitRuntimeEnvironment(git, inheritedPath))
    inheritedPath = result.env.PATH
    result.items.push({ id: 'git', label: 'Git Bash', ok: true, detail: git.bash })
    reportStage(0, 'Git Bash 已就绪', 100, 'checking', 'ready')
  } catch (error) {
    result.ok = false
    result.requiredReady = false
    result.items.push({
      id: 'git',
      label: 'Git Bash',
      ok: false,
      detail: String(error),
      blocked: isBlockedSpawn(error)
    })
    reportStage(0, 'Git Bash 准备失败', 100, 'checking', 'failed')
  }

  for (const [componentIndex, component] of manifest.components.entries()) {
    const stageIndex = componentIndex + 1
    const report: RuntimeDiagnosticReporter = event => diagnose(diagnostic, { ...event, component: component.id })
    let phase = 'probe.cached'

    reportStage(stageIndex, `正在检查 ${component.label}…`, 0, 'checking', 'check')

    if (!/^[a-z0-9-]+$/.test(component.id) || !/^[a-f0-9]{64}$/.test(component.sha256)) {
      throw new Error('Invalid runtime manifest')
    }

    const base = path.join(runtime, `${component.id}-${component.sha256.slice(0, 16)}`)

    const componentEnv = (root: string) => ({
      ...process.env,
      ...result.env,
      PATH: prependPath(
        component.pathDirs.map(dir => path.join(root, dir)),
        inheritedPath
      ),
      PYTHONDONTWRITEBYTECODE: '1'
    })

    try {
      let root = base

      try {
        const active = JSON.parse(fs.readFileSync(base + '.json', 'utf8')).directory

        if (
          typeof active === 'string' &&
          active.startsWith(path.basename(base) + '-') &&
          path.basename(active) === active
        ) {
          root = path.join(runtime, active)
        }
      } catch {
        /* First initialization has no active directory marker. */
      }

      try {
        diagnose(report, { phase, status: 'start', helper: path.join(root, component.probe) })
        await probe(component, root, componentEnv(root))
      } catch (error) {
        diagnose(report, { phase, status: 'failed', helper: path.join(root, component.probe) }, error)

        if (component.browser) {
          const browser = await findSystemBrowser(component, { ...process.env, ...result.env })

          if (browser) {
            result.env.AGENT_BROWSER_EXECUTABLE_PATH = browser
            result.items.push({ id: component.id, label: component.label, ok: true, detail: browser })
            reportStage(stageIndex, `${component.label} 已就绪`, 100, 'checking', 'ready')

            continue
          }
        }

        const system = await findSystemRuntime(component)

        if (system) {
          inheritedPath = prependPath([system], inheritedPath)
          result.env.PATH = inheritedPath
          result.items.push({ id: component.id, label: component.label, ok: true, detail: system })
          reportStage(stageIndex, `${component.label} 已就绪`, 100, 'checking', 'ready')

          continue
        }

        if (!repair) {
          throw error
        }

        phase = 'download'
        diagnose(report, { phase, status: 'start' })

        const archive = await fetchRuntime(
          component,
          cache,
          (message, percent = 0) =>
            reportStage(stageIndex, message, Math.round(percent * 0.85), 'preparing', 'download', percent),
          fetchImpl
        )

        phase = 'extract'
        root = `${base}-${crypto.randomUUID()}`
        reportStage(stageIndex, `正在准备 ${component.label}…`, 90, 'preparing', 'extract')

        try {
          if (component.format === 'file') {
            fs.mkdirSync(root, { recursive: true })
            fs.copyFileSync(archive, path.join(root, component.probe))
          } else {
            await extractRuntimeArchive({
              archive,
              destination: root,
              resources,
              prefix: component.prefix,
              format: component.format || 'zip',
              diagnostic: report
            })
          }

          if (component.browser) {
            // Chromium's AppContainer needs RX on its own binaries; keep its sandbox enabled.
            // https://pptr.dev/troubleshooting#chrome-reports-sandbox-errors-on-windows
            await run(
              system32('icacls.exe'),
              [root, '/grant', '*S-1-15-2-1:(OI)(CI)(RX)', '*S-1-15-2-2:(OI)(CI)(RX)'],
              {
                timeout: 30_000
              }
            )
          }

          phase = 'probe.prepared'
          diagnose(report, { phase, status: 'start', helper: path.join(root, component.probe) })
          await probe(component, root, componentEnv(root))
          phase = 'activate.marker'
          diagnose(report, { phase, status: 'start' })
          // A running/scanned executable can lock its directory on Windows. Activate a
          // fully verified directory by replacing only a tiny pointer, never moving it.
          const pending = base + '.' + crypto.randomUUID() + '.tmp'
          fs.writeFileSync(pending, JSON.stringify({ directory: path.basename(root) }))
          fs.renameSync(pending, base + '.json')
        } catch (error) {
          diagnose(report, { phase, status: 'failed' }, error)

          try {
            await fs.promises.rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
          } catch {
            /* Preserve the original probe error if Windows still holds a file. */
          }

          throw error
        }
      }

      inheritedPath = componentEnv(root).PATH
      result.env.PATH = inheritedPath

      if (component.browser) {
        result.env.AGENT_BROWSER_EXECUTABLE_PATH = path.join(root, component.browser)
      }

      result.items.push({ id: component.id, label: component.label, ok: true, detail: root })
      reportStage(stageIndex, `${component.label} 已就绪`, 100, 'checking', 'ready')
    } catch (error) {
      result.ok = false

      if (component.required) {
        result.requiredReady = false
      }

      diagnose(report, { phase, status: 'failed' }, error)
      result.items.push({
        id: component.id,
        label: component.label,
        ok: false,
        detail: String(error),
        blocked: isBlockedSpawn(error)
      })
      reportStage(stageIndex, `${component.label} 准备失败`, 100, 'checking', 'failed')
    }
  }

  return result
}
