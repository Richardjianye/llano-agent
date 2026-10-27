import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'
import { hashTree, assertLocalMcpCurrent, assertRunAsNode, localMcpGeneratorHash, LOCAL_MCP_ENTRY, vendorServerInfo } from './stage-local-mcp.mjs'
test('package gate detects dependency, plugin and target changes while ignoring Python caches', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'llano-mcp-gate-'))
  const runtimeRoot = path.join(root, 'runtime'), sourceRoot = path.join(root, 'source'), plugins = path.join(root, 'plugins'), dingtalk = path.join(root, 'dingtalk'), telegram = path.join(root, 'telegram')
  try {
    for (const dir of [runtimeRoot, sourceRoot, plugins, dingtalk, telegram]) fs.mkdirSync(dir)
    fs.writeFileSync(path.join(sourceRoot, 'default-mcp.json'), '{}')
    fs.writeFileSync(path.join(runtimeRoot, 'default-mcp.json'), '{}')
    fs.writeFileSync(path.join(plugins, 'plugin.py'), '# plugin')
    fs.cpSync(plugins, path.join(runtimeRoot, 'default-plugins'), { recursive: true })
    fs.writeFileSync(path.join(dingtalk, 'plugin.yaml'), 'name: dingtalk-platform')
    fs.cpSync(dingtalk, path.join(runtimeRoot, 'default-plugins/dingtalk'), { recursive: true })
    fs.writeFileSync(path.join(telegram, 'plugin.yaml'), 'name: telegram-platform')
    fs.cpSync(telegram, path.join(runtimeRoot, 'default-plugins/telegram'), { recursive: true })
    const pkg = path.join(runtimeRoot, 'node_modules/open-websearch')
    fs.mkdirSync(path.join(pkg, 'build'), { recursive: true })
    fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify({ version: '2.2.0' }))
    const entry = path.join(runtimeRoot, LOCAL_MCP_ENTRY)
    fs.writeFileSync(entry, '// MCP')
    const chunk = path.join(pkg, 'build/llano-chunks/runtime.mjs')
    fs.mkdirSync(path.dirname(chunk))
    fs.writeFileSync(chunk, '// deferred services')
    fs.writeFileSync(path.join(runtimeRoot, 'runtime-stamp.json'), JSON.stringify({ schemaVersion: 3,
      generator: localMcpGeneratorHash(), entry: LOCAL_MCP_ENTRY,
      platform: process.platform, arch: process.arch, manifest: hashTree(sourceRoot), plugins: hashTree(plugins), platforms: { dingtalk: hashTree(dingtalk), telegram: hashTree(telegram) },
      dependencies: hashTree(path.join(runtimeRoot, 'node_modules')) }))
    const options = { runtimeRoot, sourceRoot, plugins, platformPlugins: { dingtalk, telegram } }
    assertLocalMcpCurrent(options)
    fs.mkdirSync(path.join(plugins, '__pycache__'))
    fs.writeFileSync(path.join(plugins, '__pycache__/plugin.pyc'), 'cache')
    assertLocalMcpCurrent(options)
    fs.writeFileSync(path.join(runtimeRoot, 'default-plugins/dingtalk/plugin.yaml'), 'name: stale-platform')
    assert.throws(() => assertLocalMcpCurrent(options), /stale/)
    fs.cpSync(dingtalk, path.join(runtimeRoot, 'default-plugins/dingtalk'), { recursive: true })
    // Every shipped platform is checked, not just the first one.
    fs.rmSync(path.join(runtimeRoot, 'default-plugins/telegram'), { recursive: true })
    assert.throws(() => assertLocalMcpCurrent(options), /stale|ENOENT/)
    fs.cpSync(telegram, path.join(runtimeRoot, 'default-plugins/telegram'), { recursive: true })
    assertLocalMcpCurrent(options)
    assert.throws(() => assertLocalMcpCurrent({ ...options, arch: 'other' }), /stale/)
    fs.writeFileSync(entry, '// modified')
    assert.throws(() => assertLocalMcpCurrent(options), /stale/)
    fs.writeFileSync(entry, '// MCP')
    fs.writeFileSync(chunk, '// modified deferred services')
    assert.throws(() => assertLocalMcpCurrent(options), /stale/)
    fs.writeFileSync(chunk, '// deferred services')
    fs.writeFileSync(path.join(runtimeRoot, 'default-plugins/plugin.py'), '# modified')
    assert.throws(() => assertLocalMcpCurrent(options), /stale/)
  } finally { fs.rmSync(root, { recursive: true, force: true }) }
})
test('generated identity follows locked vendor MCP serverInfo and refuses ambiguous drift', () => {
  assert.deepEqual(vendorServerInfo("new McpServer({ name: 'vendor-name', version: '4.5.6' })"),
    { name: 'vendor-name', version: '4.5.6' })
  assert.throws(() => vendorServerInfo('new McpServer(dynamicConfig)'), /identity changed/)
  assert.throws(() => vendorServerInfo("new McpServer({name:'a',version:'1'})\nnew McpServer({name:'b',version:'2'})"), /identity changed/)
})
test('default runtime and packaging probe resolve the same generated entry without extending deadlines', () => {
  const defaults = JSON.parse(fs.readFileSync(new URL('../resources/local-mcp-runtime/default-mcp.json', import.meta.url), 'utf8'))
  const local = defaults.mcp_servers.llano_websearch
  assert.equal(local.args[0], `\${LLANO_LOCAL_MCP_ROOT}/${LOCAL_MCP_ENTRY}`)
  assert.equal(local.env.MODE, 'stdio')
  assert.equal(local.connect_timeout, 10)
  assert.equal(local.timeout, 30)
})
test('disabled Electron RunAsNode fuse blocks packaging', async () => {
  await assert.rejects(assertRunAsNode('/electron', async () => ({ 0: 48 })), /RunAsNode/)
  await assertRunAsNode('/electron', async () => ({ 0: 49 }))
})
