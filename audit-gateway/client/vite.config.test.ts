import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { optimizeDeps, resolveConfig } from 'vite'
import { describe, expect, it } from 'vitest'

describe('Vite gateway routing', () => {
  it('uses a server-side proxy without exposing a browser API origin', async () => {
    const config = await resolveConfig({
      configFile: fileURLToPath(new URL('vite.config.mts', import.meta.url)),
    }, 'serve', 'development')
    const adminProxy = config.server.proxy?.['/admin']

    expect(config.env.VITE_API_BASE).toBeUndefined()
    expect(
      typeof adminProxy === 'string' ? adminProxy : adminProxy?.target,
    ).toBe('http://127.0.0.1:7899')
  })

  it('pre-bundles Vuetify components used only by lazy route pages', async () => {
    const cacheDir = await mkdtemp(join(tmpdir(), 'llano-vite-test-'))
    try {
      const config = await resolveConfig({
        cacheDir,
        configFile: fileURLToPath(new URL('vite.config.mts', import.meta.url)),
      }, 'serve', 'development')
      const metadata = await optimizeDeps(config, true, true)

      expect(Object.keys(metadata.optimized)).toEqual(expect.arrayContaining([
        'vuetify/components/VDataTable',
        'vuetify/components/VTextarea',
      ]))
    } finally {
      await rm(cacheDir, { force: true, recursive: true })
    }
  }, 20_000)
})
