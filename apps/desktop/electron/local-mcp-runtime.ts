import fs from 'node:fs'
import path from 'node:path'

interface LocalMcpRuntimeOptions {
  appRoot: string
  resourcesPath: string
  isPackaged: boolean
  executable: string
  exists?: (filename: string) => boolean
}

/** Internal paths only: MCP uses the app's Node runtime and packaged dependencies. */
export function localMcpBackendEnv({
  appRoot, resourcesPath, isPackaged, executable, exists = fs.existsSync
}: LocalMcpRuntimeOptions): Record<string, string> {
  const root = path.join(isPackaged ? resourcesPath : path.join(appRoot, 'build'), 'local-mcp')
  const defaults = path.join(root, 'default-mcp.json')
  const entry = path.join(root, 'node_modules', 'open-websearch', 'build', 'llano-stdio.mjs')
  if (!exists(defaults) || !exists(entry)) {
    if (isPackaged) {
      throw new Error('Packaged local web-search runtime is missing.')
    }
    return {}
  }
  return {
    LLANO_LOCAL_MCP_CONFIG: defaults,
    LLANO_LOCAL_MCP_ROOT: root,
    LLANO_LOCAL_MCP_NODE: executable
  }
}
