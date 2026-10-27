import assert from 'node:assert/strict'

import { test } from 'vitest'

import afterAllArtifactBuild from './after-all-artifact-build.mjs'
import { verifyMacDmg } from './verify-macos-release.mjs'

function verificationAdapters(calls, { codesignFails = false } = {}) {
  return {
    exists: () => true,
    hashFile: async () => 'expected-sha256',
    isExecutable: () => true,
    writeFile: async (filePath, contents) => calls.push({ command: 'write', filePath, contents }),
    run: async (command, args) => {
      calls.push({ command, args })
      if (command === 'hdiutil' && args[0] === 'attach') {
        return { stdout: '<plist><dict/></plist>', stderr: '' }
      }
      if (command === 'plutil') {
        return {
          stdout: JSON.stringify({ 'system-entities': [{ 'mount-point': '/Volumes/Internal' }] }),
          stderr: ''
        }
      }
      if (command === 'codesign' && codesignFails) {
        throw new Error('signature invalid')
      }
      return { stdout: '', stderr: '' }
    }
  }
}

test('DMG verification checks the mounted app before hashing', async () => {
  const calls = []

  const result = await verifyMacDmg('/release/app.dmg', verificationAdapters(calls))

  assert.deepEqual(
    calls.filter(call => call.command !== 'write').map(call => call.command),
    ['hdiutil', 'hdiutil', 'plutil', 'codesign', 'hdiutil']
  )
  assert.deepEqual(calls[3].args, [
    '--verify',
    '--deep',
    '--strict',
    '--verbose=4',
    '/Volumes/Internal/LLANO Agent.app'
  ])
  assert.equal(result.sha256, 'expected-sha256')
  assert.equal(result.checksumPath, '/release/app.dmg.sha256')
  assert.equal(calls.at(-1).contents, 'expected-sha256  app.dmg\n')
})

test('detach still runs when mounted app verification fails', async () => {
  const calls = []

  await assert.rejects(
    () => verifyMacDmg('/release/app.dmg', verificationAdapters(calls, { codesignFails: true })),
    /signature invalid/
  )

  const lastCommand = calls.filter(call => call.command !== 'write').at(-1)
  assert.equal(lastCommand.command, 'hdiutil')
  assert.deepEqual(lastCommand.args, ['detach', '/Volumes/Internal'])
})

test('DMG verification refuses a mounted image without the double-click helper', async () => {
  const calls = []
  const adapters = verificationAdapters(calls)
  adapters.exists = filePath => !filePath.endsWith('安装完成后双击这里.command')

  await assert.rejects(
    () => verifyMacDmg('/release/app.dmg', adapters),
    /does not contain 安装完成后双击这里\.command/
  )

  const lastCommand = calls.filter(call => call.command !== 'write').at(-1)
  assert.equal(lastCommand.command, 'hdiutil')
  assert.deepEqual(lastCommand.args, ['detach', '/Volumes/Internal'])
})

test('DMG verification refuses a non-executable double-click helper', async () => {
  const calls = []
  const adapters = verificationAdapters(calls)
  adapters.isExecutable = () => false

  await assert.rejects(
    () => verifyMacDmg('/release/app.dmg', adapters),
    /is not executable/
  )
})

test('artifact hook verifies only DMGs and publishes checksum sidecars', async () => {
  const verified = []

  const result = await afterAllArtifactBuild(
    { artifactPaths: ['/release/app.zip', '/release/app.dmg', '/release/app.blockmap'] },
    {
      verify: async artifact => {
        verified.push(artifact)
        return { checksumPath: `${artifact}.sha256`, sha256: 'sha' }
      }
    }
  )

  assert.deepEqual(verified, ['/release/app.dmg'])
  assert.deepEqual(result, ['/release/app.dmg.sha256'])
})
