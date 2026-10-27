import { describe, expect, it } from 'vitest'

import {
  bytesToBase64,
  parseSkillExists,
  payloadFromFolder,
  payloadFromMarkdown,
  payloadFromZip,
  SkillTooLargeError
} from './import-skill-files'

function fileOf(text: string | Uint8Array, name: string, relativePath = ''): File {
  const file = new File([text as BlobPart], name)

  if (relativePath) {
    Object.defineProperty(file, 'webkitRelativePath', { value: relativePath })
  }

  return file
}

const decode = (value: string) => new TextDecoder().decode(Uint8Array.from(atob(value), ch => ch.charCodeAt(0)))

describe('bytesToBase64', () => {
  it('round-trips bytes larger than one chunk', () => {
    const bytes = Uint8Array.from({ length: 0x8000 * 2 + 17 }, (_, index) => index % 251)

    expect(Uint8Array.from(atob(bytesToBase64(bytes)), ch => ch.charCodeAt(0))).toEqual(bytes)
  })
})

describe('payloadFromMarkdown', () => {
  it('sends any .md as SKILL.md, whatever it is called', async () => {
    const payload = await payloadFromMarkdown(fileOf('---\nname: x\n---\n', 'template.md'))

    expect(Object.keys(payload.files)).toEqual(['SKILL.md'])
    expect(decode(payload.files['SKILL.md']!)).toContain('name: x')
  })
})

describe('payloadFromFolder', () => {
  it('drops the outermost folder name and keeps sub-paths', async () => {
    const payload = await payloadFromFolder([
      fileOf('a', 'SKILL.md', '会计凭证编制/SKILL.md'),
      fileOf('b', 'a.md', '会计凭证编制/references/a.md')
    ])

    expect(Object.keys(payload.files).sort()).toEqual(['SKILL.md', 'references/a.md'])
  })

  it('refuses more than 64 files or more than 4 MB', async () => {
    const many = Array.from({ length: 65 }, (_, index) => fileOf('x', `f${index}.md`, `d/f${index}.md`))

    await expect(payloadFromFolder(many)).rejects.toBeInstanceOf(SkillTooLargeError)
    await expect(
      payloadFromFolder([fileOf(new Uint8Array(4 * 1024 * 1024 + 1), 'big.bin', 'd/big.bin')])
    ).rejects.toBeInstanceOf(SkillTooLargeError)
  })
})

describe('payloadFromZip', () => {
  it('sends the archive whole and leaves unpacking to the backend', async () => {
    const payload = await payloadFromZip(fileOf('PK-not-really', '技能.zip'))

    expect(payload.files).toEqual({})
    expect(decode(payload.archive!)).toBe('PK-not-really')
  })
})

describe('parseSkillExists', () => {
  it('reads the 409 detail after the desktop wraps it', () => {
    const error = new Error(
      `Error invoking remote method 'hermes:api': Error: 409: {"detail":"SKILL_EXISTS::invoice-helper::发票助手::Skill 'invoice-helper' already exists（已经有一个叫「发票助手」的技能了）"}`
    )

    expect(parseSkillExists(error)).toEqual({ displayName: '发票助手', name: 'invoice-helper' })
  })

  it('copes with a colon inside the display name', () => {
    expect(parseSkillExists(new Error('SKILL_EXISTS::a-b::甲: 乙::x'))).toEqual({ displayName: '甲: 乙', name: 'a-b' })
  })

  it('ignores every other error', () => {
    expect(parseSkillExists(new Error('400: {"detail":"这不是有效的 .zip 压缩包"}'))).toBeNull()
    expect(parseSkillExists(undefined)).toBeNull()
  })
})
