import { test, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { SIDECAR_STAMP, sourceFingerprint, binaryFingerprint, assertSidecarCurrent } from './sidecar-stamp.mjs'

test('pack rejects stale, modified and wrong-target backends while allowing UI-only changes', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sidecar-source-'))
  try {
    execFileSync('git', ['init', '-q', root])
    fs.writeFileSync(path.join(root, 'run_agent.py'), 'original')
    const sidecarDir = path.join(root, 'build')
    fs.mkdirSync(sidecarDir)
    const binary = path.join(sidecarDir, 'llano-sidecar')
    fs.writeFileSync(binary, 'frozen-backend')
    const args = { repoRoot: root, sidecarDir, binary, platform: 'darwin', arch: 'arm64' }
    expect(() => assertSidecarCurrent(args)).toThrow('源码记录')
    const stamp = { schemaVersion: 1, source: sourceFingerprint(root), binary: binaryFingerprint(binary), platform: 'darwin', arch: 'arm64' }
    fs.writeFileSync(path.join(sidecarDir, SIDECAR_STAMP), JSON.stringify(stamp))
    expect(() => assertSidecarCurrent(args)).not.toThrow()
    fs.mkdirSync(path.join(root, 'apps/desktop/src'), { recursive: true })
    fs.writeFileSync(path.join(root, 'apps/desktop/src/page.tsx'), 'changed-ui')
    expect(() => assertSidecarCurrent(args)).not.toThrow()
    expect(() => assertSidecarCurrent({ ...args, platform: 'win32', arch: 'x64' })).toThrow('不匹配')
    fs.writeFileSync(path.join(root, 'run_agent.py'), 'changed')
    expect(() => assertSidecarCurrent(args)).toThrow('过期')
    fs.writeFileSync(path.join(root, 'run_agent.py'), 'original')
    fs.writeFileSync(path.join(root, 'new_runtime.py'), 'new dependency')
    expect(() => assertSidecarCurrent(args)).toThrow('过期')
    fs.unlinkSync(path.join(root, 'new_runtime.py'))
    fs.writeFileSync(binary, 'wrong-binary')
    expect(() => assertSidecarCurrent(args)).toThrow('过期')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('brand default skill additions and edits invalidate the bundled sidecar stamp', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'brand-skill-source-'))
  try {
    execFileSync('git', ['init', '-q', root])
    const before = sourceFingerprint(root)
    const skill = path.join(root, 'apps/desktop/resources/default-skills/local-web-search/SKILL.md')
    fs.mkdirSync(path.dirname(skill), { recursive: true })
    fs.writeFileSync(skill, 'first version')
    const added = sourceFingerprint(root)
    expect(added).not.toBe(before)
    fs.writeFileSync(skill, 'updated Chinese guidance')
    expect(sourceFingerprint(root)).not.toBe(added)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})


test('reviewed Chinese catalog changes invalidate the sidecar source stamp', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'catalog-stamp-'))
  try {
    execFileSync('git', ['init', '-q', root])
    fs.mkdirSync(path.join(root, 'tools'))
    const original = sourceFingerprint(root)
    fs.writeFileSync(path.join(root, 'tools/bundled_skill_chinese_catalog.json'), '{}')
    expect(sourceFingerprint(root)).not.toBe(original)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
