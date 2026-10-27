import assert from 'node:assert/strict'
import { test } from 'vitest'
import { runAfterPack } from './after-pack.mjs'
const context = { electronPlatformName: 'win32', appOutDir: '/packed', packager: { appInfo: { productFilename: 'Workbench' } } }
const policy = { brandId: 'llano', productName: 'LLANO Agent' }
test('branded Windows rejects stamping failure before any executable probe', async () => {
  let probed = false, warned = false
  await assert.rejects(runAfterPack(context, { policy, stamp: async () => { throw new Error('resource editor denied') }, verifyMcp: async () => { probed = true }, warn: () => { warned = true } }), /resource editor denied/)
  assert.equal(probed, false); assert.equal(warned, false)
})
test('branded Windows rejects silent incorrect resources even after stamp success', async () => {
  let probed = false
  await assert.rejects(runAfterPack(context, { policy, stamp: async () => {}, verifyIdentity: () => { throw new Error('icon mismatch') }, verifyMcp: async () => { probed = true } }), /icon mismatch/)
  assert.equal(probed, false)
})
test('Windows stamps and checks the final executable before MCP executes it', async () => {
  const calls = []
  await runAfterPack(context, { policy, stamp: async exe => calls.push(['stamp', exe]), verifyIdentity: (exe, icon, product, version) => { calls.push(['identity', exe]); assert.equal(product, policy.productName); assert.match(version, /^\d+\.\d+\.\d+$/) }, verifyMcp: async () => { calls.push(['mcp']) } })
  assert.deepEqual(calls, [['stamp', '/packed/Workbench.exe'], ['identity', '/packed/Workbench.exe'], ['mcp']])
})
test('sidecarless development option cannot bypass branded PE verification', async () => {
  await assert.rejects(runAfterPack(context, { policy, allowSidecarless: true, stamp: async () => { throw new Error('stamp failed') } }), /stamp failed/)
})
test('stock builds retain best-effort stamping and skip branded verification', async () => {
  let warned = false
  await runAfterPack(context, { policy: { ...policy, brandId: '' }, stamp: async () => { throw new Error('unavailable') }, verifyIdentity: () => { throw new Error('should not verify stock') }, warn: () => { warned = true } })
  assert.equal(warned, true)
})
test('macOS keeps its MCP gate and does not edit PE resources', async () => {
  let probed = false
  await runAfterPack({ ...context, electronPlatformName: 'darwin' }, { policy, stamp: async () => { throw new Error('not Windows') }, verifyMcp: async () => { probed = true } })
  assert.equal(probed, true)
})

test('resource editor receives brand ICO, identity and actual package release versions', async () => {
  const { stampExeIdentity } = await import('./set-exe-identity.mjs')
  const fs = await import('node:fs/promises'), os = await import('node:os'), path = await import('node:path')
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'llano-stamp-test-'))
  try {
    await fs.mkdir(path.join(root, 'assets')); await fs.writeFile(path.join(root, 'assets/icon.ico'), 'test icon')
    await fs.writeFile(path.join(root, 'package.json'), JSON.stringify({ version: '0.18.6' }))
    const exe = path.join(root, 'Workbench.exe'); await fs.writeFile(exe, 'test executable')
    let options
    await stampExeIdentity(exe, root, { policy: { ...policy, companyName: 'LLANO' }, edit: async (target, opts) => { assert.equal(target, exe); options = opts } })
    assert.equal(options.icon, path.join(root, 'assets/icon.ico'))
    assert.equal(options['file-version'], '0.18.6'); assert.equal(options['product-version'], '0.18.6')
    assert.equal(options['version-string'].ProductName, policy.productName); assert.equal(options['version-string'].FileDescription, policy.productName)
    await assert.rejects(stampExeIdentity(exe, root, { policy, edit: async () => { throw new Error('editor failed') } }), /editor failed/)
  } finally { await fs.rm(root, { recursive: true, force: true }) }
})
