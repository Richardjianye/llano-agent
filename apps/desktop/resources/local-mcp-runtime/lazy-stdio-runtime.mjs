/** Keep vendor registration synchronous; load services once at the first call. */
export function registerLazyRuntime(server, setupTools, config, loadRuntime) {
  const runtime = { config, services: undefined }
  let initialization
  const ensureRuntime = () => (initialization ??= Promise.resolve().then(loadRuntime).then(real => {
    runtime.services = real.services
  }))
  const originalTool = server.tool
  server.tool = function (name, ...rest) {
    const handler = rest.pop()
    if (typeof handler !== 'function') throw new TypeError(`Invalid vendor handler for ${name}`)
    return originalTool.call(this, name, ...rest, async function (...args) {
      await ensureRuntime()
      return handler.apply(this, args)
    })
  }
  setupTools(server, runtime)
  return runtime
}
