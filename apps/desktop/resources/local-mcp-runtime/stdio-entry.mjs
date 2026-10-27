import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { setupTools } from './tools/setupTools.js'
import { config } from './config.js'
import { registerLazyRuntime } from 'llano:lazy-runtime'

// This generated desktop entry only serves local stdio, never an HTTP daemon.
if (process.env.MODE !== 'stdio') throw new Error('Local desktop MCP requires MODE=stdio')
const server = new McpServer(__LLANO_SERVER_INFO__)
registerLazyRuntime(server, setupTools, config, async () => {
  const { createOpenWebSearchRuntime } = await import('./runtime/createRuntime.js')
  return createOpenWebSearchRuntime({ config })
})
await server.connect(new StdioServerTransport())
