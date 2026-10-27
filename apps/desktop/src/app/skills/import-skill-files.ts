// CUSTOM（2026-09-21，决策 #59）：把同事在「＋ 添加技能」里选的文件夹 / SKILL.md / .zip
// 变成后端 /api/skills/hub/import 要的载荷。这一层只管读文件和卡大小，
// 校验、扫描、命名都在后端做，界面不重复判断（重复了迟早两边对不上）。

export const MAX_SKILL_FILES = 64
export const MAX_SKILL_BYTES = 4 * 1024 * 1024
// 压缩包本身比解压后的上限宽一点（与后端 MAX_ARCHIVE_BYTES 对齐），解压后仍按 4 MB 卡
const MAX_ARCHIVE_BYTES = 6 * 1024 * 1024

export interface ImportPayload {
  archive?: string
  files: Record<string, string>
}

/** 文件夹太大 / 文件太多：文案由调用方给（走 i18n），这里只负责区分是这一类错误。 */
export class SkillTooLargeError extends Error {}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  const chunk = 0x8000

  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk))
  }

  return btoa(binary)
}

async function readBase64(file: File): Promise<string> {
  return bytesToBase64(new Uint8Array(await file.arrayBuffer()))
}

/** 文件夹：路径去掉最外层目录名，其余原样交给后端。 */
export async function payloadFromFolder(selected: File[]): Promise<ImportPayload> {
  if (selected.length > MAX_SKILL_FILES || selected.reduce((sum, file) => sum + file.size, 0) > MAX_SKILL_BYTES) {
    throw new SkillTooLargeError()
  }

  const root = selected[0]!.webkitRelativePath.split('/')[0]!
  const files: Record<string, string> = {}

  for (const file of selected) {
    files[file.webkitRelativePath.slice(root.length + 1)] = await readBase64(file)
  }

  return { files }
}

/** 单个 .md：不管它叫什么名字（template.md、skill.md……），都当作这个技能的 SKILL.md。 */
export async function payloadFromMarkdown(file: File): Promise<ImportPayload> {
  if (file.size > MAX_SKILL_BYTES) {
    throw new SkillTooLargeError()
  }

  return { files: { 'SKILL.md': await readBase64(file) } }
}

/** .zip：整个 base64 发给后端，由后端在安全限制下解压（zip-slip、符号链接、解压炸弹）。 */
export async function payloadFromZip(file: File): Promise<ImportPayload> {
  if (file.size > MAX_ARCHIVE_BYTES) {
    throw new SkillTooLargeError()
  }

  return { archive: await readBase64(file), files: {} }
}

/**
 * 后端对「已经有同名技能」返回 409，detail 形如
 * `SKILL_EXISTS::标识::展示名::说明`。界面据此问「要覆盖吗」，而不是弹一条报错。
 */
export function parseSkillExists(error: unknown): { displayName: string; name: string } | null {
  const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : ''
  const match = raw.match(/SKILL_EXISTS::([a-z0-9._-]+)::(.*?)::/s)

  return match ? { displayName: match[2] || match[1]!, name: match[1]! } : null
}
