import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { getCurrentFuseWire, FuseV1Options } from '@electron/fuses'
import { build, version as esbuildVersion } from 'esbuild'

import { desktopRoot } from './product-environment.mjs'

export const LOCAL_MCP_VERSION = '2.2.0'
export const LOCAL_MCP_ENTRY = 'node_modules/open-websearch/build/llano-stdio.mjs'
const manifestRoot = path.join(desktopRoot, 'resources', 'local-mcp-runtime')
const pluginRoot = path.join(desktopRoot, 'resources', 'default-plugins')
// Messaging platforms the branded client ships. Each one's Python deps must
// also be frozen into the sidecar (build-sidecar/requirements.txt).
export const SHIPPED_PLATFORM_PLUGINS = ['dingtalk', 'feishu', 'telegram']
const platformPluginRoots = Object.fromEntries(SHIPPED_PLATFORM_PLUGINS.map(name =>
  [name, path.resolve(desktopRoot, '..', '..', 'plugins', 'platforms', name)]))
const hashPlatforms = roots => Object.fromEntries(Object.entries(roots).map(([name, root]) => [name, hashTree(root)]))
export const localMcpRoot = path.join(desktopRoot, 'build', 'local-mcp')

export function localMcpGeneratorHash() {
  return createHash('sha256').update(fs.readFileSync(fileURLToPath(import.meta.url))).update(esbuildVersion).digest('hex')
}

export function vendorServerInfo(source) {
  const matches = [...source.matchAll(/new McpServer\(\{\s*name:\s*(['"])([^'"]+)\1\s*,\s*version:\s*(['"])([^'"]+)\3\s*\}\)/g)]
  if (matches.length !== 1) throw new Error('Locked vendor MCP server identity changed')
  return { name: matches[0][2], version: matches[0][4] }
}

export async function bundleLocalMcp({ runtimeRoot = localMcpRoot, sourceRoot = manifestRoot } = {}) {
  const vendorBuild = path.join(runtimeRoot, 'node_modules', 'open-websearch', 'build')
  const identity = vendorServerInfo(fs.readFileSync(path.join(vendorBuild, 'index.js'), 'utf8'))
  const contents = fs.readFileSync(path.join(sourceRoot, 'stdio-entry.mjs'), 'utf8').replace('__LLANO_SERVER_INFO__', JSON.stringify(identity))
  let workerTransforms = 0
  await build({
    stdin: { contents, resolveDir: vendorBuild, sourcefile: 'llano-stdio-entry.mjs', loader: 'js' },
    outdir: vendorBuild, entryNames: 'llano-stdio',
    chunkNames: 'llano-chunks/[name]-[hash]', outExtension: { '.js': '.mjs' },
    bundle: true, splitting: true, platform: 'node', format: 'esm', target: 'node22', minify: true,
    external: ['canvas', 'koffi', 'playwright', 'playwright-core', 'chromium-bidi', 'electron'],
    banner: { js: 'import {createRequire as __llanoCreateRequire} from "node:module"; const require=__llanoCreateRequire(import.meta.url);' },
    plugins: [{ name: 'preserve-vendor-resource-locations', setup(builder) {
      builder.onResolve({ filter: /^llano:lazy-runtime$/ }, () => ({ path: path.join(sourceRoot, 'lazy-stdio-runtime.mjs') }))
      builder.onLoad({ filter: /XMLHttpRequest-impl\.js$/ }, ({ path: filename }) => {
        const original = fs.readFileSync(filename, 'utf8')
        const marker = 'require.resolve("./xhr-sync-worker.js")'
        if (original.split(marker).length !== 2) throw new Error('Locked JSDOM worker resolver changed')
        workerTransforms++
        return { contents: original.replace(marker, 'require.resolve("jsdom/lib/jsdom/living/xhr/xhr-sync-worker.js")'),
          loader: 'js', resolveDir: path.dirname(filename) }
      })
    } }]
  })
  if (workerTransforms !== 1) throw new Error('Local MCP must retain exactly one JSDOM worker resolver')
}

export function hashTree(root, excludedRootEntries = []) {
  const hash = createHash('sha256')
  function visit(dir, prefix = '') {
    for (const name of fs.readdirSync(dir).sort()) {
      // builder-util's copy walker always omits these metadata files, before
      // evaluating any FileSet filter. Hash the actual shippable payload.
      if (name === '.gitkeep' || name === '.DS_Store') continue
      if (name === '__pycache__' || /\.py[co]$/.test(name)) continue
      if (!prefix && excludedRootEntries.includes(name)) continue
      const filename = path.join(dir, name)
      const relative = prefix ? `${prefix}/${name}` : name
      const stat = fs.lstatSync(filename)
      if (stat.isSymbolicLink()) {
        hash.update(relative).update('\0').update(fs.readlinkSync(filename)).update('\0')
      } else if (stat.isDirectory()) {
        visit(filename, relative)
      } else {
        hash.update(relative).update('\0').update(fs.readFileSync(filename)).update('\0')
      }
    }
  }
  visit(root)
  return hash.digest('hex')
}

export function assertLocalMcpCurrent({ runtimeRoot = localMcpRoot, sourceRoot = manifestRoot, plugins = pluginRoot, platformPlugins = platformPluginRoots, platform = process.platform, arch = process.arch } = {}) {
  const stamp = JSON.parse(fs.readFileSync(path.join(runtimeRoot, 'runtime-stamp.json'), 'utf8'))
  const pkg = JSON.parse(fs.readFileSync(path.join(runtimeRoot, 'node_modules', 'open-websearch', 'package.json'), 'utf8'))
  const names = Object.keys(platformPlugins)
  if (stamp.schemaVersion !== 3 || stamp.generator !== localMcpGeneratorHash() || stamp.entry !== LOCAL_MCP_ENTRY || pkg.version !== LOCAL_MCP_VERSION || stamp.platform !== platform || stamp.arch !== arch ||
      stamp.manifest !== hashTree(sourceRoot) || stamp.plugins !== hashTree(plugins) ||
      stamp.dependencies !== hashTree(path.join(runtimeRoot, 'node_modules')) ||
      stamp.plugins !== hashTree(path.join(runtimeRoot, 'default-plugins'), names) ||
      JSON.stringify(Object.keys(stamp.platforms || {}).sort()) !== JSON.stringify([...names].sort()) ||
      names.some(name => stamp.platforms[name] !== hashTree(platformPlugins[name]) ||
        stamp.platforms[name] !== hashTree(path.join(runtimeRoot, 'default-plugins', name))) ||
      fs.readFileSync(path.join(runtimeRoot, 'default-mcp.json'), 'utf8') !== fs.readFileSync(path.join(sourceRoot, 'default-mcp.json'), 'utf8') ||
      !fs.existsSync(path.join(runtimeRoot, LOCAL_MCP_ENTRY))) {
    throw new Error('Local web-search runtime is missing or stale. Run npm run stage:local-mcp before packaging.')
  }
}

export async function stageLocalMcp() {
  const signature = hashTree(manifestRoot)
  const plugins = hashTree(pluginRoot)
  const platforms = hashPlatforms(platformPluginRoots)
  try {
    assertLocalMcpCurrent()
    console.log('[local-mcp] verified locked runtime; no install needed')
    return
  } catch { /* Build-time staging is the only place npm may install this runtime. */ }
  fs.mkdirSync(localMcpRoot, { recursive: true })
  for (const name of ['package.json', 'package-lock.json', 'default-mcp.json']) {
    fs.copyFileSync(path.join(manifestRoot, name), path.join(localMcpRoot, name))
  }
  const npmCli = process.env.npm_execpath
  if (!npmCli || !fs.existsSync(npmCli)) {
    throw new Error('Run this build-time stage through npm run stage:local-mcp so the npm CLI is explicit.')
  }
  execFileSync(process.execPath, [npmCli, 'ci', '--prefix', localMcpRoot, '--workspaces=false', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'], {
    stdio: 'inherit', timeout: 180_000
  })
  // Koffi publishes prebuilt bindings for every platform. Ship this target
  // only; retain the package and dependency licenses without modification.
  const bindings = path.join(localMcpRoot, 'node_modules', 'koffi', 'build', 'koffi')
  const target = `${process.platform}_${process.arch}`
  if (!fs.existsSync(path.join(bindings, target))) throw new Error(`No locked Koffi binding for ${target}`)
  for (const directory of fs.readdirSync(bindings)) {
    if (directory !== target) fs.rmSync(path.join(bindings, directory), { recursive: true, force: true })
  }
  fs.rmSync(path.join(localMcpRoot, 'default-plugins'), { recursive: true, force: true })
  fs.cpSync(pluginRoot, path.join(localMcpRoot, 'default-plugins'), {
    recursive: true,
    filter: source => !source.split(path.sep).includes('__pycache__') && !/\.py[co]$/.test(source)
  })
  // Platform plugins need their actual manifest and Python files on disk;
  // PyInstaller's import archive is not a discoverable plugin directory.
  for (const [name, root] of Object.entries(platformPluginRoots)) {
    fs.cpSync(root, path.join(localMcpRoot, 'default-plugins', name), {
      recursive: true,
      filter: source => !source.split(path.sep).includes('__pycache__') && !/\.py[co]$/.test(source)
    })
  }
  fs.copyFileSync(path.resolve(desktopRoot, '..', '..', 'LICENSE'), path.join(localMcpRoot, 'LICENSE.hermes-platforms'))
  // Keep locked vendor files, dynamic dependencies, native binaries and licenses
  // intact. Only generated assets are added; first-call services live in chunks.
  await bundleLocalMcp()
  fs.writeFileSync(path.join(localMcpRoot, 'runtime-stamp.json'), `${JSON.stringify({
    schemaVersion: 3, version: LOCAL_MCP_VERSION, manifest: signature, plugins, platforms,
    generator: localMcpGeneratorHash(), entry: LOCAL_MCP_ENTRY,
    platform: process.platform, arch: process.arch,
    dependencies: hashTree(path.join(localMcpRoot, 'node_modules'))
  }, null, 2)}\n`)
  assertLocalMcpCurrent()
  console.log(`[local-mcp] staged open-websearch ${LOCAL_MCP_VERSION}; runtime installs disabled`)
}

export async function assertRunAsNode(executable, readFuses = getCurrentFuseWire) {
  const fuses = await readFuses(executable)
  if (fuses[FuseV1Options.RunAsNode] !== 49) {
    throw new Error('Electron RunAsNode is disabled; the packaged local MCP cannot start.')
  }
}

export async function probeLocalMcp({ runtimeRoot = localMcpRoot, executable } = {}) {
  const require = createRequire(path.join(runtimeRoot, 'package.json'))
  const { Client } = require('@modelcontextprotocol/sdk/client/index.js')
  const { StdioClientTransport } = require('@modelcontextprotocol/sdk/client/stdio.js')
  const electron = executable || createRequire(path.join(desktopRoot, 'package.json'))('electron')
  await assertRunAsNode(electron)
  const client = new Client({ name: 'llano-local-runtime-check', version: '1.0.0' })
  const transport = new StdioClientTransport({
    command: electron,
    args: [path.join(runtimeRoot, LOCAL_MCP_ENTRY)],
    env: { ELECTRON_RUN_AS_NODE: '1', MODE: 'stdio', SEARCH_MODE: 'request' },
    stderr: 'pipe'
  })
  let timer
  try {
    const tools = await Promise.race([
      (async () => { await client.connect(transport); return client.listTools() })(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Local MCP stdio initialization timed out')), 15_000) })
    ])
    for (const name of ['search', 'fetchWebContent']) {
      if (!tools.tools.some(tool => tool.name === name)) throw new Error(`Local MCP is missing ${name}`)
    }
    return { initialized: true, tools: tools.tools.map(tool => tool.name) }
  } finally {
    clearTimeout(timer)
    await client.close()
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await stageLocalMcp()
