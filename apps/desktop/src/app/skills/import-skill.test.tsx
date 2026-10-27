// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { queryClient } from '@/lib/query-client'

const importDownloadedSkill = vi.fn()
const notify = vi.fn()
const notifyError = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  importDownloadedSkill: (...args: unknown[]) => importDownloadedSkill(...args)
}))

vi.mock('@/store/notifications', () => ({
  notify: (input: unknown) => notify(input),
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  queryClient.clear()
})

async function renderButton(onImported = vi.fn()) {
  const { ImportSkillButton } = await import('./import-skill')

  const view = render(
    <QueryClientProvider client={queryClient}>
      <ImportSkillButton onImported={onImported} />
    </QueryClientProvider>
  )

  return {
    input: (slot: string) => view.container.querySelector(`[data-slot="${slot}"]`) as HTMLInputElement,
    onImported
  }
}

async function choose(input: HTMLInputElement, file: File) {
  Object.defineProperty(input, 'files', { configurable: true, value: [file] })

  await act(async () => {
    fireEvent.change(input)
  })
}

const markdown = new File(['---\nname: x\ndescription: y\n---\n'], 'template.md')

describe('ImportSkillButton', () => {
  it('adds a single SKILL.md, tells the colleague where it landed, and hands the new name back', async () => {
    importDownloadedSkill.mockResolvedValue({
      display_name: '发票助手',
      file_count: 1,
      name: 'invoice-helper',
      success: true,
      verdict: 'safe'
    })
    const { input, onImported } = await renderButton()

    await choose(input('import-skill-file'), markdown)

    await waitFor(() => expect(onImported).toHaveBeenCalledWith('invoice-helper'))
    const [name, files, options] = importDownloadedSkill.mock.calls[0]!

    expect(name).toBe('')
    expect(Object.keys(files as object)).toEqual(['SKILL.md'])
    expect(options).toEqual({ archive: undefined, overwrite: false })
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'success', title: 'Added "发票助手"' }))
  })

  it('sends a .zip through the archive field', async () => {
    importDownloadedSkill.mockResolvedValue({ file_count: 2, name: 'sample', success: true, verdict: 'safe' })
    const { input } = await renderButton()

    await choose(input('import-skill-zip'), new File(['PK'], '技能.zip'))

    await waitFor(() => expect(importDownloadedSkill).toHaveBeenCalled())
    expect(importDownloadedSkill.mock.calls[0]![1]).toEqual({})
    expect(importDownloadedSkill.mock.calls[0]![2].archive).toBeTruthy()
  })

  it('asks before overwriting, and only retries with overwrite after the colleague says yes', async () => {
    importDownloadedSkill
      .mockRejectedValueOnce(
        new Error('409: {"detail":"SKILL_EXISTS::sample::发票助手::Skill \'sample\' already exists"}')
      )
      .mockResolvedValueOnce({
        display_name: '发票助手',
        file_count: 1,
        name: 'sample',
        replaced: true,
        success: true,
        verdict: 'safe'
      })
    const { input, onImported } = await renderButton()

    await choose(input('import-skill-file'), markdown)

    expect(await screen.findByText('"发票助手" is already installed')).toBeTruthy()
    expect(importDownloadedSkill).toHaveBeenCalledTimes(1) // 没问就不覆盖
    expect(notifyError).not.toHaveBeenCalled() // 这不是一条报错

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace' }))
    })

    await waitFor(() => expect(onImported).toHaveBeenCalledWith('sample'))
    expect(importDownloadedSkill.mock.calls[1]![2].overwrite).toBe(true)
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ title: 'Updated "发票助手"' }))
  })

  it('does nothing more when the colleague cancels the overwrite', async () => {
    importDownloadedSkill.mockRejectedValue(new Error('409: {"detail":"SKILL_EXISTS::sample::sample::x"}'))
    const { input, onImported } = await renderButton()

    await choose(input('import-skill-file'), markdown)
    await screen.findByText('"sample" is already installed')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    })

    expect(importDownloadedSkill).toHaveBeenCalledTimes(1)
    expect(onImported).not.toHaveBeenCalled()
  })

  it('shows any other failure as an error', async () => {
    importDownloadedSkill.mockRejectedValue(new Error('400: {"detail":"这不是有效的 .zip 压缩包"}'))
    const { input } = await renderButton()

    await choose(input('import-skill-zip'), new File(['x'], 'bad.zip'))

    await waitFor(() => expect(notifyError).toHaveBeenCalled())
    expect(notifyError.mock.calls[0]![1]).toBe('Could not add the skill')
  })

  it('never asks the colleague to type Chinese; an AI miss still adds the skill with a gentle warning', async () => {
    importDownloadedSkill.mockResolvedValue({
      display_name: 'weekly-report',
      file_count: 1,
      metadata_completed: false,
      metadata_error: 'AI 中文补齐未完成',
      metadata_incomplete: true,
      name: 'weekly-report',
      success: true,
      verdict: 'safe'
    })
    const { input, onImported } = await renderButton()

    await choose(input('import-skill-file'), markdown)

    await waitFor(() => expect(onImported).toHaveBeenCalledWith('weekly-report'))
    expect(importDownloadedSkill).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'warning', title: 'Added "weekly-report"' }))
    expect(notifyError).not.toHaveBeenCalled()
  })
})
