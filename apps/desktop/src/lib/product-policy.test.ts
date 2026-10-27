import { describe, expect, it } from 'vitest'

import {
  isAllowedMessagingPlatform,
  isCapabilityAllowed,
  isEnvVarAllowed,
  PRODUCT_POLICY,
  type ProductPolicy,
  visibleSettingsSections
} from './product-policy'

const policy = (disabledCapabilities: ProductPolicy['disabledCapabilities']): ProductPolicy => ({
  ...PRODUCT_POLICY,
  disabledCapabilities
})

describe('branded product capability policy', () => {
  it('keeps every messaging connector visible without a platform allowlist', () => {
    const unrestricted = { ...PRODUCT_POLICY, allowedMessagingPlatforms: [] }

    for (const platform of ['feishu', 'wecom', 'slack', 'discord', 'new-plugin-channel']) {
      expect(isAllowedMessagingPlatform(platform, unrestricted)).toBe(true)
    }
    for (const platform of ['feishu', 'wecom', 'slack', 'discord']) {
      expect(isAllowedMessagingPlatform(platform)).toBe(true)
    }
    expect(isCapabilityAllowed('provider_management', unrestricted)).toBe(false)
    expect(isCapabilityAllowed('nous_services', unrestricted)).toBe(false)
  })

  it('retains explicit connector restrictions when a nonempty list is configured', () => {
    const restricted = { ...PRODUCT_POLICY, allowedMessagingPlatforms: ['telegram'] }

    expect(isAllowedMessagingPlatform(' Telegram ', restricted)).toBe(true)
    expect(isAllowedMessagingPlatform('discord', restricted)).toBe(false)
  })

  it('blocks only the capabilities declared by the immutable product policy', () => {
    expect(isCapabilityAllowed('pet_ai_generation')).toBe(false)
    expect(isCapabilityAllowed('nous_services')).toBe(false)
  })

  it('keeps ordinary settings while removing provider/model settings', () => {
    const sections = [{ id: 'model' }, { id: 'appearance' }, { id: 'workspace' }, { id: 'advanced' }]

    expect(visibleSettingsSections(sections).map(section => section.id)).toEqual([
      'appearance',
      'workspace',
      'advanced'
    ])
    expect(visibleSettingsSections(sections, policy([]))).toEqual(sections)
  })

  it('hides only image-only credentials when built-in image generation is disabled', () => {
    expect(isEnvVarAllowed('KREA_API_KEY')).toBe(false)
    expect(isEnvVarAllowed('FAL_KEY')).toBe(true)
    expect(isEnvVarAllowed('KREA_API_KEY', policy([]))).toBe(true)
  })
})
