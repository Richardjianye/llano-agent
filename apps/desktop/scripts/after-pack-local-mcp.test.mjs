import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { test } from 'vitest'
import { verifyPackagedLocalMcp } from './after-pack.mjs'
import { hashTree } from './stage-local-mcp.mjs'
const require = createRequire(import.meta.url)
const { getFileMatchers, copyFiles } = require('app-builder-lib/out/fileMatcher')
const config = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8')).build

test('actual electron-builder extraResources copy retains full external npm runtime', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'llano-mcp-resources-'))
  try {
    const source = path.join(root, 'build/local-mcp')
    const files = ['package.json', 'default-mcp.json', 'node_modules/open-websearch/build/index.js',
      'node_modules/open-websearch/build/llano-stdio.mjs', 'node_modules/open-websearch/build/llano-chunks/runtime.mjs',
      'node_modules/jsdom/lib/jsdom/living/xhr/xhr-sync-worker.js', 'node_modules/@mozilla/readability/index.js',
      'node_modules/open-websearch/LICENSE', 'node_modules/@modelcontextprotocol/sdk/package.json',
      'node_modules/koffi/build/koffi/darwin_arm64/koffi.node',
      'node_modules/vendor/node_modules/nested/index.js']
    for (const file of files) {
      const filename = path.join(source, file)
      fs.mkdirSync(path.dirname(filename), { recursive: true })
      fs.writeFileSync(filename, file)
    }
    // builder-util's walker drops these before FileSet filtering. They are
    // packaging metadata, not runtime dependencies; the fingerprint must agree.
    fs.writeFileSync(path.join(source, 'node_modules/vendor/.gitkeep'), '')
    fs.writeFileSync(path.join(source, 'node_modules/vendor/.DS_Store'), 'finder metadata')
    const options = { defaultSrc: root, globalOutDir: path.join(root, 'dist'),
      macroExpander: value => value, customBuildOptions: {} }
    const destination = path.join(root, 'resources')
    const localSets = config.extraResources.filter(set => set.from.startsWith('build/local-mcp'))
    // Reproduce the real builder defect: a FileSet rooted at local-mcp skips its root node_modules.
    const broken = getFileMatchers({ extraResources: [localSets[0]] }, 'extraResources', destination, options)
    await copyFiles(broken, undefined, false)
    assert.equal(fs.existsSync(path.join(destination, 'local-mcp/node_modules')), false)
    fs.rmSync(destination, { recursive: true, force: true })
    // The dedicated dependency FileSet changes the traversal root and includes nested packages/licenses/native binaries.
    await copyFiles(getFileMatchers({ extraResources: localSets }, 'extraResources', destination, options), undefined, false)
    for (const file of files) {
      assert.equal(fs.readFileSync(path.join(destination, 'local-mcp', file), 'utf8'), file)
    }
    assert.equal(hashTree(path.join(source, 'node_modules')),
      hashTree(path.join(destination, 'local-mcp/node_modules')))
  } finally { fs.rmSync(root, { recursive: true, force: true }) }
})

test('afterPack validates copied resources and probes the actual macOS app executable', async () => {
  const calls = []
  await verifyPackagedLocalMcp({ appOutDir: '/packed', electronPlatformName: 'darwin',
    packager: { appInfo: { productFilename: 'Workbench' } } }, {
    verifyRuntime: options => calls.push(['runtime', options]),
    verifyFuse: async executable => calls.push(['fuse', executable]),
    probe: async options => calls.push(['probe', options])
  })
  const runtimeRoot = '/packed/Workbench.app/Contents/Resources/local-mcp'
  const executable = '/packed/Workbench.app/Contents/MacOS/Workbench'
  assert.deepEqual(calls, [['runtime', { runtimeRoot }], ['fuse', executable], ['probe', { runtimeRoot, executable }]])
})

test('a missing copied dependency aborts afterPack before a successful source probe can hide it', async () => {
  let probed = false
  await assert.rejects(verifyPackagedLocalMcp({ appOutDir: '/packed', electronPlatformName: 'win32' }, {
    verifyRuntime: () => { throw new Error('missing copied dependencies') },
    probe: async () => { probed = true }
  }), /missing copied dependencies/)
  assert.equal(probed, false)
})
