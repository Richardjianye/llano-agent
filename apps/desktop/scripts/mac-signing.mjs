import fs from 'node:fs'
import path from 'node:path'
import { execFile } from 'node:child_process'

import { signAsync as electronSignAsync } from '@electron/osx-sign'

function runCommand(command, args) {
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

function envValue(env, name) {
  return String(env?.[name] || '').trim()
}

function publisherSigningConfigured(env = process.env) {
  return Boolean(envValue(env, 'CSC_LINK') || envValue(env, 'APPLE_SIGNING_IDENTITY'))
}

function notaryConfiguration(env = process.env) {
  const profile = envValue(env, 'APPLE_NOTARY_PROFILE')
  if (profile) {
    return { mode: 'profile', profile }
  }

  const key = envValue(env, 'APPLE_API_KEY')
  const keyId = envValue(env, 'APPLE_API_KEY_ID')
  const issuer = envValue(env, 'APPLE_API_ISSUER')
  const configured = [key, keyId, issuer].filter(Boolean).length

  if (configured === 0) {
    return { mode: 'none' }
  }
  if (configured !== 3) {
    const missing = [
      ['APPLE_API_KEY', key],
      ['APPLE_API_KEY_ID', keyId],
      ['APPLE_API_ISSUER', issuer]
    ]
      .filter(([, value]) => !value)
      .map(([name]) => name)
      .join(', ')
    throw new Error(`Apple API notary credentials are partially configured; missing: ${missing}`)
  }

  return { mode: 'api', key, keyId, issuer }
}

async function prepareMacAppForDistribution(context, adapters = {}) {
  const env = adapters.env || process.env
  const exists = adapters.exists || fs.existsSync
  const signAsync = adapters.signAsync || electronSignAsync
  const run = adapters.run || runCommand
  const appName = context.packager?.appInfo?.productFilename

  if (!appName) {
    throw new Error('Cannot prepare macOS distribution without packager.appInfo.productFilename')
  }

  const appPath = path.join(context.appOutDir, `${appName}.app`)
  if (!exists(appPath)) {
    throw new Error(`Cannot prepare missing app bundle: ${appPath}`)
  }

  if (!publisherSigningConfigured(env)) {
    const desktopRoot = path.resolve(import.meta.dirname, '..')
    const mainEntitlements = path.join(desktopRoot, 'electron', 'entitlements.mac.plist')
    const inheritedEntitlements = path.join(
      desktopRoot,
      'electron',
      'entitlements.mac.inherit.plist'
    )

    if (!exists(mainEntitlements) || !exists(inheritedEntitlements)) {
      throw new Error(`Cannot sign macOS app: entitlement files are missing under ${desktopRoot}/electron`)
    }

    await signAsync({
      app: appPath,
      identity: '-',
      identityValidation: false,
      platform: 'darwin',
      preAutoEntitlements: false,
      preEmbedProvisioningProfile: false,
      strictVerify: true,
      optionsForFile(filePath) {
        return {
          entitlements: filePath === appPath ? mainEntitlements : inheritedEntitlements,
          hardenedRuntime: true,
          timestamp: 'none'
        }
      }
    })
  }

  await run('codesign', ['--verify', '--deep', '--strict', '--verbose=4', appPath])
  return appPath
}

export {
  notaryConfiguration,
  prepareMacAppForDistribution,
  publisherSigningConfigured,
  runCommand
}
