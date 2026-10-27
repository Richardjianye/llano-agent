import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFile } from 'node:child_process'

import { notaryConfiguration, prepareMacAppForDistribution } from './mac-signing.mjs'

function run(command, args) {
  return new Promise((resolve, reject) => {
    execFile(command, args, (error, stdout, stderr) => {
      if (error) {
        reject(
          new Error(
            `${command} ${args.join(' ')} failed: ${stderr?.trim() || stdout?.trim() || error.message}`
          )
        )
        return
      }
      resolve({ stdout, stderr })
    })
  })
}

function inlineKeyLooksValid(value) {
  return value.includes('BEGIN PRIVATE KEY') && value.includes('END PRIVATE KEY')
}

function resolveApiKeyPath(rawValue) {
  const value = String(rawValue || '').trim()
  if (!value) return { keyPath: '', cleanup: () => {} }

  if (fs.existsSync(value)) {
    return { keyPath: value, cleanup: () => {} }
  }

  if (!inlineKeyLooksValid(value)) {
    throw new Error('APPLE_API_KEY must be a file path or inline .p8 key content')
  }

  const tempPath = path.join(os.tmpdir(), `hermes-notary-${Date.now()}-${process.pid}.p8`)
  fs.writeFileSync(tempPath, value, 'utf8')
  return {
    keyPath: tempPath,
    cleanup: () => {
      try {
        fs.rmSync(tempPath, { force: true })
      } catch {
        // Best-effort cleanup.
      }
    }
  }
}

export default async function notarize(context) {
  const { electronPlatformName, appOutDir, packager } = context
  if (electronPlatformName !== 'darwin') return

  const appName = packager.appInfo.productFilename
  const appPath = await prepareMacAppForDistribution(context)
  const notary = notaryConfiguration(process.env)

  if (notary.mode === 'profile') {
    const zipPath = path.join(appOutDir, `${appName}.zip`)
    await run('ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', appPath, zipPath])
    await run('xcrun', [
      'notarytool',
      'submit',
      zipPath,
      '--keychain-profile',
      notary.profile,
      '--wait'
    ])
    await run('xcrun', ['stapler', 'staple', '-v', appPath])
    try {
      fs.rmSync(zipPath, { force: true })
    } catch {
      // Best-effort cleanup.
    }
    return
  }

  if (notary.mode === 'none') {
    console.log('Skipping notarization: verified internal ad-hoc distribution profile.')
    return
  }

  const { keyPath, cleanup } = resolveApiKeyPath(notary.key)
  const zipPath = path.join(appOutDir, `${appName}.zip`)
  try {
    await run('ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', appPath, zipPath])
    await run('xcrun', [
      'notarytool',
      'submit',
      zipPath,
      '--key',
      keyPath,
      '--key-id',
      notary.keyId,
      '--issuer',
      notary.issuer,
      '--wait'
    ])
    await run('xcrun', ['stapler', 'staple', '-v', appPath])
  } finally {
    try {
      fs.rmSync(zipPath, { force: true })
    } catch {
      // Best-effort cleanup.
    }
    cleanup()
  }
}
