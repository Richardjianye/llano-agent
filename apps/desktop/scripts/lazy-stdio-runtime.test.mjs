import assert from 'node:assert/strict'
import { test } from 'vitest'
import { registerLazyRuntime } from '../resources/local-mcp-runtime/lazy-stdio-runtime.mjs'

test('vendor registration stays synchronous and concurrent first calls share one real runtime', async () => {
  const tools = new Map()
  const server = { tool(name, description, schema, handler) {
    assert.equal(this, server)
    tools.set(name, { description, schema, handler })
  } }
  const config = { allowedSearchEngines: [], defaultSearchEngine: 'bing' }
  const schema = { input: { type: 'string' } }
  const receiver = { value: 'callback receiver' }
  let factories = 0
  let release
  const barrier = new Promise(resolve => { release = resolve })
  const service = { value: 'real service', execute(input) {
    assert.equal(this, service)
    return `${this.value}:${input}`
  } }
  const runtime = registerLazyRuntime(server, (vendor, state) => {
    assert.equal(state.config, config)
    vendor.tool('vendor-tool', 'vendor description', schema, function (input, extra) {
      assert.equal(this, receiver)
      assert.equal(extra, 'unchanged extra')
      return state.services.actual.execute(input)
    })
  }, config, async () => {
    factories++
    await barrier
    return { services: { actual: service } }
  })
  assert.equal(factories, 0)
  assert.equal(runtime.services, undefined)
  const tool = tools.get('vendor-tool')
  const before = JSON.stringify({ description: tool.description, schema: tool.schema })
  const first = tool.handler.call(receiver, 'one', 'unchanged extra')
  const second = tool.handler.call(receiver, 'two', 'unchanged extra')
  await Promise.resolve()
  assert.equal(factories, 1)
  release()
  assert.deepEqual(await Promise.all([first, second]), ['real service:one', 'real service:two'])
  assert.equal(await tool.handler.call(receiver, 'three', 'unchanged extra'), 'real service:three')
  assert.equal(factories, 1)
  assert.equal(runtime.services.actual, service)
  assert.equal(tool.schema, schema)
  assert.equal(JSON.stringify({ description: tool.description, schema: tool.schema }), before)
})

test('invalid vendor callbacks fail during registration without loading services', () => {
  let loaded = false
  assert.throws(() => registerLazyRuntime({ tool() { throw new Error('must not register') } }, vendor => {
    vendor.tool('broken', 'description', {}, null)
  }, {}, () => { loaded = true }), /Invalid vendor handler/)
  assert.equal(loaded, false)
})

test('original tool failures propagate without rebuilding runtime or changing services', async () => {
  let handler
  let factories = 0
  const expected = new Error('original service failure')
  registerLazyRuntime({ tool(_name, callback) { handler = callback } }, (vendor, runtime) => {
    vendor.tool('fails', () => runtime.services.actual.execute())
  }, {}, async () => {
    factories++
    return { services: { actual: { execute() { throw expected } } } }
  })
  await assert.rejects(handler(), error => error === expected)
  await assert.rejects(handler(), error => error === expected)
  assert.equal(factories, 1)
})
