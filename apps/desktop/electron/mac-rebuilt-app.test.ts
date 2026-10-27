import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { macRebuiltAppCandidates } from './mac-rebuilt-app'

test('renamed macOS bundle candidates cover arm64 and fallback output dirs', () => {
  assert.deepEqual(macRebuiltAppCandidates('/repo', 'LLANO Agent'), [
    path.join('/repo', 'apps', 'desktop', 'release', 'mac-arm64', 'LLANO Agent.app'),
    path.join('/repo', 'apps', 'desktop', 'release', 'mac', 'LLANO Agent.app')
  ])
})
