import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

export const SIDECAR_STAMP = 'source-stamp.json'

// Hash runtime inputs, including new untracked modules. Documentation-only or
// renderer changes should not force another Python freeze.
export function sourceFingerprint(repoRoot) {
  const files = execFileSync('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], {
    cwd: repoRoot, encoding: 'utf8'
  }).split('\0').filter(Boolean)
  const inputs = [...new Set(files)].filter(file =>
    (file.endsWith('.py') && !/^(tests|audit-gateway|scripts|website|apps)\//.test(file)) ||
    /^(skills|locales)\//.test(file) ||
    file === 'tools/bundled_skill_chinese_catalog.json' ||
    /^apps\/desktop\/resources\/default-skills\//.test(file) ||
    /^(pyproject\.toml|uv\.lock)$/.test(file) ||
    /^apps\/desktop\/build-sidecar\//.test(file) ||
    /^apps\/desktop\/scripts\/(build-sidecar|sidecar-stamp)\.mjs$/.test(file)
  ).sort()
  const hash = createHash('sha256')
  for (const file of inputs) {
    hash.update(file).update('\0')
    const full = path.join(repoRoot, file)
    hash.update(fs.existsSync(full) ? fs.readFileSync(full) : '<deleted>').update('\0')
  }
  return hash.digest('hex')
}

export function binaryFingerprint(binary) {
  return createHash('sha256').update(fs.readFileSync(binary)).digest('hex')
}

export function assertSidecarCurrent({ repoRoot, sidecarDir, binary, platform, arch }) {
  let stamp
  try {
    stamp = JSON.parse(fs.readFileSync(path.join(sidecarDir, SIDECAR_STAMP), 'utf8'))
  } catch {
    throw new Error('[before-pack] sidecar 缺少有效源码记录，请重新运行 npm run build:sidecar。')
  }
  if (stamp.schemaVersion !== 1 || stamp.platform !== platform || stamp.arch !== arch ||
      stamp.source !== sourceFingerprint(repoRoot) || stamp.binary !== binaryFingerprint(binary)) {
    throw new Error('[before-pack] sidecar 已过期或目标平台/架构不匹配，请在目标机器重新运行 npm run build:sidecar。')
  }
}
