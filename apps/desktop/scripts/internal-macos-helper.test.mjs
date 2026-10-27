import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { afterEach, test } from 'vitest'

const helperPath = fileURLToPath(new URL('./安装完成后双击这里.command', import.meta.url))
const temporaryRoots = []

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

function createSignedApp({ tampered = false } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'llano-internal-helper-'))
  temporaryRoots.push(root)
  const appPath = path.join(root, 'LLANO Agent.app')
  const contentsPath = path.join(appPath, 'Contents')
  const macosPath = path.join(contentsPath, 'MacOS')
  const executablePath = path.join(macosPath, 'LLANO Agent')

  fs.mkdirSync(macosPath, { recursive: true })
  fs.writeFileSync(
    path.join(contentsPath, 'Info.plist'),
    `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>LLANO Agent</string>
  <key>CFBundleIdentifier</key><string>com.llano.test-internal-helper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
`
  )
  fs.writeFileSync(executablePath, '#!/bin/sh\nexit 0\n', { mode: 0o755 })
  const signed = spawnSync('/usr/bin/codesign', ['--force', '--deep', '--sign', '-', appPath], {
    encoding: 'utf8'
  })
  assert.equal(signed.status, 0, signed.stderr)

  if (tampered) {
    fs.appendFileSync(executablePath, '# signature must now be invalid\n')
  }

  const quarantined = spawnSync(
    '/usr/bin/xattr',
    ['-w', 'com.apple.quarantine', '0081;00000000;LlanoTests;', appPath],
    { encoding: 'utf8' }
  )
  assert.equal(quarantined.status, 0, quarantined.stderr)
  const unrelatedMetadata = spawnSync(
    '/usr/bin/xattr',
    ['-w', 'com.llano.testmetadata', 'keep-me', appPath],
    { encoding: 'utf8' }
  )
  assert.equal(unrelatedMetadata.status, 0, unrelatedMetadata.stderr)
  return appPath
}

function quarantineStatus(appPath) {
  return spawnSync('/usr/bin/xattr', ['-p', 'com.apple.quarantine', appPath], {
    encoding: 'utf8'
  }).status
}

test.runIf(process.platform === 'darwin')(
  'double-click helper removes only quarantine from a valid signed app',
  () => {
    const appPath = createSignedApp()

    const result = spawnSync(helperPath, [appPath], { encoding: 'utf8' })

    assert.equal(result.status, 0, result.stderr || result.stdout)
    assert.equal(quarantineStatus(appPath), 1)
    const unrelatedMetadata = spawnSync(
      '/usr/bin/xattr',
      ['-p', 'com.llano.testmetadata', appPath],
      { encoding: 'utf8' }
    )
    assert.equal(unrelatedMetadata.status, 0, unrelatedMetadata.stderr)
    assert.equal(unrelatedMetadata.stdout.trim(), 'keep-me')
    const verified = spawnSync(
      '/usr/bin/codesign',
      ['--verify', '--deep', '--strict', appPath],
      { encoding: 'utf8' }
    )
    assert.equal(verified.status, 0, verified.stderr)
  }
)

test.runIf(process.platform === 'darwin')(
  'double-click helper refuses a tampered app without clearing quarantine',
  () => {
    const appPath = createSignedApp({ tampered: true })

    const result = spawnSync(helperPath, [appPath], { encoding: 'utf8' })

    assert.notEqual(result.status, 0)
    assert.equal(quarantineStatus(appPath), 0)
  }
)
