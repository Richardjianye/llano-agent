/**
 * stock Hermes 构建下的措辞表 —— 「不许一刀切」那一条
 *
 * 单独一个文件是因为 `IS_BRANDED_PRODUCT` 在模块加载时就定死了，
 * 要测非品牌构建只能整个文件 `vi.mock` 掉 product-policy。
 *
 * 为什么值得单独测：`Connection error.` 和 `redaction policy expired` 不一样。
 * 后者只可能出自我们自己的 fail-closed，天然自限；前者在 stock Hermes 里
 * 可以来自用户自己配的任何一个 provider —— 一律说成「连不上公司网关」就是撒谎，
 * 而且是那种看起来很正常、没人会去质疑的谎。
 *
 * 同一条思路在网关侧的绕过矩阵里也有（「只禁用别的能力时必须放行」那一条）：
 * 规则得真的按条件生效，而不是「凡是品牌相关的都套同一句话」。
 */

import { describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/product-policy', () => ({
  IS_BRANDED_PRODUCT: false,
  PRODUCT_POLICY: { brandId: '' }
}))

const { summarizeErrorMessage } = await import('@/store/notifications')

describe('stock 构建：不能把任意 provider 的连接失败说成公司网关', () => {
  it('Connection error. 保持原样', () => {
    expect(summarizeErrorMessage('Connection error.', 'fallback')).toBe('Connection error.')
  })

  it('redaction policy 那条不受构建影响 —— 它只可能出自我们自己', () => {
    const shown = summarizeErrorMessage('redaction policy expired', 'fallback')

    expect(shown).not.toMatch(/redaction policy/i)
  })
})
