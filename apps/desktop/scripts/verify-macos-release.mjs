import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { spawn } from 'node:child_process'

import { productExecutableName } from './utils.mjs'

function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ['pipe', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''

    child.stdout.on('data', chunk => {
      stdout += chunk.toString()
    })
    child.stderr.on('data', chunk => {
      stderr += chunk.toString()
    })
    child.on('error', reject)
    child.on('close', code => {
      if (code !== 0) {
        reject(
          new Error(
            `${command} ${args.join(' ')} failed (${code}): ${stderr.trim() || stdout.trim()}`
          )
        )
        return
      }
      resolve({ stdout, stderr })
    })

    if (options.input != null) {
      child.stdin.end(String(options.input))
    } else {
      child.stdin.end()
    }
  })
}

function hashFile(filePath) {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha256')
    const stream = fs.createReadStream(filePath)
    stream.on('error', reject)
    stream.on('data', chunk => hash.update(chunk))
    stream.on('end', () => resolve(hash.digest('hex')))
  })
}

function mountPointFromPlistJson(raw) {
  const parsed = JSON.parse(String(raw || '{}'))
  const entity = parsed['system-entities']?.find(item => item?.['mount-point'])
  const mountPoint = entity?.['mount-point']
  if (!mountPoint) {
    throw new Error('hdiutil attach did not report a mounted volume')
  }
  return mountPoint
}

function fileIsExecutable(filePath) {
  try {
    fs.accessSync(filePath, fs.constants.X_OK)
    return true
  } catch {
    return false
  }
}

async function verifyMacDmg(dmgPath, adapters = {}) {
  const exists = adapters.exists || fs.existsSync
  const run = adapters.run || runCommand
  const calculateHash = adapters.hashFile || hashFile
  const isExecutable = adapters.isExecutable || fileIsExecutable
  const writeFile = adapters.writeFile || fs.promises.writeFile
  const productName = adapters.productName || productExecutableName('darwin')
  const helperName = adapters.helperName || '安装完成后双击这里.command'

  if (!exists(dmgPath)) {
    throw new Error(`Cannot verify missing DMG: ${dmgPath}`)
  }

  await run('hdiutil', ['verify', dmgPath])
  const attached = await run('hdiutil', ['attach', '-readonly', '-nobrowse', '-plist', dmgPath])
  const converted = await run('plutil', ['-convert', 'json', '-o', '-', '-'], {
    input: attached.stdout
  })
  const mountPoint = mountPointFromPlistJson(converted.stdout)
  const appPath = path.join(mountPoint, `${productName}.app`)
  const helperPath = path.join(mountPoint, helperName)
  let verificationError = null

  try {
    if (!exists(appPath)) {
      throw new Error(`Mounted DMG does not contain ${productName}.app: ${dmgPath}`)
    }
    if (!exists(helperPath)) {
      throw new Error(`Mounted DMG does not contain ${helperName}: ${dmgPath}`)
    }
    if (!isExecutable(helperPath)) {
      throw new Error(`Mounted DMG helper is not executable: ${helperPath}`)
    }
    await run('codesign', ['--verify', '--deep', '--strict', '--verbose=4', appPath])
  } catch (error) {
    verificationError = error
  }

  try {
    await run('hdiutil', ['detach', mountPoint])
  } catch (detachError) {
    if (!verificationError) {
      throw detachError
    }
  }

  if (verificationError) {
    throw verificationError
  }

  const sha256 = await calculateHash(dmgPath)
  const checksumPath = `${dmgPath}.sha256`
  await writeFile(checksumPath, `${sha256}  ${path.basename(dmgPath)}\n`, 'utf8')

  return { checksumPath, sha256 }
}

export { fileIsExecutable, hashFile, mountPointFromPlistJson, runCommand, verifyMacDmg }
