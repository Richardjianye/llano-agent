import { describe, expect, it } from 'vitest'

import { productNetworkUserAgent } from './product-user-agent'

describe('productNetworkUserAgent', () => {
  it('keeps the localized display name out of HTTP and WebSocket headers', () => {
    const result = productNetworkUserAgent(
      'Mozilla/5.0 LLANO Agent/0.17.0 Chrome/144 Electron/40',
      'LLANO Agent',
      'llano-agent',
      '0.17.0'
    )

    expect(result).toBe('Mozilla/5.0 llano-agent/0.17.0 Chrome/144 Electron/40')
    expect(result).toMatch(/^[\x20-\x7E]+$/)
  })

  it('falls back to an ASCII product id when the brand id has no wire-safe characters', () => {
    expect(productNetworkUserAgent('产品/1.0', '产品', '品牌', '1.0')).toBe('HermesDesktop/1.0')
  })
})
