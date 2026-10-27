import assert from 'node:assert/strict'
import fs from 'node:fs'

import { test } from 'vitest'

import { productExecutableName } from './utils.mjs'

test('macOS uses the installed Chinese application name only on macOS', () => {
  assert.equal(productExecutableName('darwin'), 'LLANO Agent')
  assert.equal(productExecutableName('win32'), 'LlanoAIWorkbench')
  assert.equal(productExecutableName('linux'), 'LlanoAIWorkbench')
})

test('package identity keeps compatibility boundaries', () => {
  const pkg = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))

  assert.equal(pkg.build.appId, 'com.llano.aiworkbench')
  assert.equal(pkg.build.executableName, 'LlanoAIWorkbench')
  assert.equal(pkg.build.mac.executableName, 'LLANO Agent')
  assert.match(pkg.build.artifactName, /^LlanoAIWorkbench-/)
})
