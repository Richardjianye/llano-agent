import { useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { importDownloadedSkill } from '@/hermes'
import { useI18n } from '@/i18n'
import { invalidateSlashCompletions } from '@/lib/slash-completion-cache'
import { notify, notifyError } from '@/store/notifications'

import {
  type ImportPayload,
  parseSkillExists,
  payloadFromFolder,
  payloadFromMarkdown,
  payloadFromZip,
  SkillTooLargeError
} from './import-skill-files'

interface Conflict {
  displayName: string
  payload: ImportPayload
}

/**
 * CUSTOM（2026-09-21，决策 #59）：技能页的「＋ 添加技能」。
 *
 * 原来叫「导入下载的技能」、藏在「浏览技能中心」页签的工具栏里，有两个问题：入口深；
 * 而且放在「技能中心」底下，同事以为是把技能传到技能中心。实际是装到自己这台电脑上，
 * 分享给同事是技能详情里的「发布」。所以入口搬到「技能」页，说明写在按钮旁边。
 *
 * 三种来源：文件夹、.zip、单个 SKILL.md。同名时后端回 409，这里问一句「覆盖吗」，
 * 确认后带 overwrite 重试（后端会先备份旧版）。
 *
 * 2026-10-16：缺中文名称或简介时不再弹框让同事手填（负责人定：同事不会花这个时间）。
 * 后端在安全扫描通过后用 AI 补齐并写进 SKILL.md；AI 没补成也照常添加，这里提示一句，
 * 列表上方的「用 AI 补全中文」可以重试。
 */
export function ImportSkillButton({ onImported }: { onImported?: (name: string) => void }) {
  const { t } = useI18n()
  const s = t.skills.import
  const cache = useQueryClient()
  const folderInput = useRef<HTMLInputElement>(null)
  const zipInput = useRef<HTMLInputElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [conflict, setConflict] = useState<Conflict | null>(null)

  const submit = async (payload: ImportPayload, overwrite = false) => {
    setBusy(true)

    try {
      const result = await importDownloadedSkill('', payload.files, { archive: payload.archive, overwrite })

      const shown = result.display_name || result.name

      notify({
        kind: result.metadata_incomplete ? 'warning' : 'success',
        title: result.replaced ? s.updated(shown) : s.added(shown),
        message: result.metadata_incomplete ? s.metadataLater : s.addedHint
      })
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['skills-list'] }),
        cache.invalidateQueries({ queryKey: ['skill-hub-sources'] })
      ])
      invalidateSlashCompletions()
      onImported?.(result.name)
    } catch (error) {
      const exists = parseSkillExists(error)

      if (exists && !overwrite) {
        setConflict({ displayName: exists.displayName, payload })
      } else {
        notifyError(error, s.failed)
      }
    } finally {
      setBusy(false)
    }
  }

  const pick = async (input: HTMLInputElement, build: (files: File[]) => Promise<ImportPayload>) => {
    // 先把 FileList 拷成数组再清空 input：清空之后同事才能再次选同一个文件
    const selected = Array.from(input.files ?? [])
    input.value = ''

    if (!selected.length) {
      return
    }

    let payload: ImportPayload

    try {
      payload = await build(selected)
    } catch (error) {
      notifyError(error instanceof SkillTooLargeError ? new Error(t.skills.hub.importSizeLimit) : error, s.failed)

      return
    }

    await submit(payload)
  }

  return (
    <>
      <input
        data-slot="import-skill-folder"
        hidden
        multiple
        onChange={event => void pick(event.currentTarget, payloadFromFolder)}
        ref={folderInput}
        type="file"
        {...{ webkitdirectory: '' }}
      />
      <input
        accept=".zip,application/zip"
        data-slot="import-skill-zip"
        hidden
        onChange={event => void pick(event.currentTarget, files => payloadFromZip(files[0]!))}
        ref={zipInput}
        type="file"
      />
      <input
        accept=".md,text/markdown"
        data-slot="import-skill-file"
        hidden
        onChange={event => void pick(event.currentTarget, files => payloadFromMarkdown(files[0]!))}
        ref={fileInput}
        type="file"
      />

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button disabled={busy} size="xs" title={s.caption} variant="secondary">
            {busy ? s.adding : s.add}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56" sideOffset={6}>
          <DropdownMenuItem onSelect={() => folderInput.current?.click()}>{s.menuFolder}</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => zipInput.current?.click()}>{s.menuZip}</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => fileInput.current?.click()}>{s.menuFile}</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog onOpenChange={open => !open && setConflict(null)} open={conflict !== null}>
        <DialogContent className="max-w-md">
          {conflict && (
            <>
              <DialogHeader>
                <DialogTitle>{s.overwriteTitle(conflict.displayName)}</DialogTitle>
                <DialogDescription>{s.overwriteBody}</DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <Button onClick={() => setConflict(null)} size="sm" variant="text">
                  {s.cancel}
                </Button>
                <Button
                  onClick={() => {
                    const pending = conflict

                    setConflict(null)
                    void submit(pending.payload, true)
                  }}
                  size="sm"
                >
                  {s.overwrite}
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
