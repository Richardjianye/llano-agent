import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { RuntimeEnvironmentPanel } from './runtime-environment-panel'
vi.mock('@/lib/product-policy', () => ({ IS_BRANDED_PRODUCT: true }))
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
it('keeps doctor read-only and requests repair only from the repair button', async () => {
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Windows')

  const doctor = vi.fn().mockResolvedValue({
    supported: true,
    ok: false,
    intranet: true,
    items: [{ id: 'git', label: 'Git Bash', ok: false, detail: 'Missing executable' }]
  })

  vi.stubGlobal('hermesDesktop', { runtimeDoctor: doctor })
  render(<RuntimeEnvironmentPanel />)
  fireEvent.click(screen.getByText('检查环境'))
  await waitFor(() => expect(doctor).toHaveBeenCalledWith(false))
  await screen.findByText('Git Bash：需要修复')
  fireEvent.click(screen.getByText('修复缺失项'))
  await waitFor(() => expect(doctor).toHaveBeenLastCalledWith(true))
  await screen.findByText('修复检查已完成。新增环境在重启工作台后生效。')
})
