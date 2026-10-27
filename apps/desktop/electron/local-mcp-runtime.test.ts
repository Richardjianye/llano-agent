import assert from 'node:assert/strict'

import { test } from 'vitest'

import { localMcpBackendEnv } from './local-mcp-runtime'
const options = { appRoot: '/source', resourcesPath: '/resources', executable: '/app/electron', isPackaged: true }
test('packaged MCP uses Electron and resource-local dependency paths without changing whole backend Node mode', () => {
  const env = localMcpBackendEnv({ ...options, exists: () => true })
  assert.equal(env.LLANO_LOCAL_MCP_ROOT, '/resources/local-mcp')
  assert.equal(env.LLANO_LOCAL_MCP_NODE, options.executable)
  assert.equal(env.ELECTRON_RUN_AS_NODE, undefined)
})
test('missing packaged MCP fails closed while unstaged development still starts', () => {
  assert.throws(() => localMcpBackendEnv({ ...options, exists: () => false }), /missing/)
  assert.deepEqual(localMcpBackendEnv({ ...options, isPackaged: false, exists: () => false }), {})
})
test('runtime availability requires the generated stdio entry rather than just the vendor CLI', () => {
  const visited: string[] = []
  localMcpBackendEnv({ ...options, exists: filename => {
    visited.push(filename)

    return true
  } })
  assert.deepEqual(visited, ['/resources/local-mcp/default-mcp.json',
    '/resources/local-mcp/node_modules/open-websearch/build/llano-stdio.mjs'])
})
