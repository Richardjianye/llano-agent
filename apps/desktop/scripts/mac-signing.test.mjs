import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  notaryConfiguration,
  prepareMacAppForDistribution,
  publisherSigningConfigured
} from './mac-signing.mjs'

function fakeContext() {
  return {
    electronPlatformName: 'darwin',
    appOutDir: '/release/mac-arm64',
    packager: {
      appInfo: {
        productFilename: 'LLANO Agent'
      }
    }
  }
}

test('internal profile uses app-aware ad-hoc signing then strict verification', async () => {
  const calls = []

  const appPath = await prepareMacAppForDistribution(fakeContext(), {
    env: {},
    exists: () => true,
    signAsync: async options => calls.push(['sign', options]),
    run: async (command, args) => calls.push([command, args])
  })

  assert.equal(appPath, path.join('/release/mac-arm64', 'LLANO Agent.app'))
  assert.equal(calls[0][0], 'sign')
  assert.equal(calls[0][1].identity, '-')
  assert.equal(calls[0][1].identityValidation, false)
  assert.equal(calls[0][1].platform, 'darwin')
  assert.equal(calls[0][1].preAutoEntitlements, false)
  assert.equal(calls[0][1].preEmbedProvisioningProfile, false)
  assert.deepEqual(calls.at(-1), [
    'codesign',
    ['--verify', '--deep', '--strict', '--verbose=4', appPath]
  ])
})

test('publisher profile skips ad-hoc signing but still strictly verifies', async () => {
  const calls = []

  await prepareMacAppForDistribution(fakeContext(), {
    env: { CSC_LINK: '/secure/developer-id.p12' },
    exists: () => true,
    signAsync: async options => calls.push(['sign', options]),
    run: async (command, args) => calls.push([command, args])
  })

  assert.deepEqual(calls, [
    [
      'codesign',
      [
        '--verify',
        '--deep',
        '--strict',
        '--verbose=4',
        path.join('/release/mac-arm64', 'LLANO Agent.app')
      ]
    ]
  ])
})

test('missing app bundle fails before signing or verification', async () => {
  const calls = []

  await assert.rejects(
    prepareMacAppForDistribution(fakeContext(), {
      env: {},
      exists: () => false,
      signAsync: async options => calls.push(['sign', options]),
      run: async (command, args) => calls.push([command, args])
    }),
    /missing app bundle/
  )
  assert.deepEqual(calls, [])
})

test('publisher identity detection ignores blank values', () => {
  assert.equal(publisherSigningConfigured({}), false)
  assert.equal(publisherSigningConfigured({ CSC_LINK: '  ' }), false)
  assert.equal(publisherSigningConfigured({ APPLE_SIGNING_IDENTITY: 'Developer ID' }), true)
})

test('notary configuration distinguishes none, profile, and complete API credentials', () => {
  assert.deepEqual(notaryConfiguration({}), { mode: 'none' })
  assert.deepEqual(notaryConfiguration({ APPLE_NOTARY_PROFILE: 'company-notary' }), {
    mode: 'profile',
    profile: 'company-notary'
  })
  assert.deepEqual(
    notaryConfiguration({
      APPLE_API_KEY: '/secure/AuthKey.p8',
      APPLE_API_KEY_ID: 'KEY123',
      APPLE_API_ISSUER: 'ISSUER123'
    }),
    {
      mode: 'api',
      key: '/secure/AuthKey.p8',
      keyId: 'KEY123',
      issuer: 'ISSUER123'
    }
  )
})

test('partial API notary credentials fail instead of silently skipping', () => {
  assert.throws(
    () => notaryConfiguration({ APPLE_API_KEY_ID: 'KEY123' }),
    /partially configured/
  )
})
