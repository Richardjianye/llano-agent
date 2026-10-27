import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Dialog, DialogContent, DialogTitle } from './dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './select'

afterEach(cleanup)

// jsdom lacks these; Radix Select calls them on open.
window.HTMLElement.prototype.scrollIntoView = vi.fn()
window.HTMLElement.prototype.hasPointerCapture = vi.fn()
window.HTMLElement.prototype.releasePointerCapture = vi.fn()

const flushTimers = async () => {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 10))
  })
}

describe('REPRO: dismissing an open Select inside a Dialog', () => {
  it.each([false, true])('closes only the select on an overlay press (initially open: %s)', async initiallyOpen => {
    // While a modal Select is open, the dialog content div is pointer-events:
    // none, so a real click anywhere in the dialog body hit-tests through to
    // the overlay. Simulate that by firing the pointer sequence on the overlay.
    const onOpenChange = vi.fn()
    render(
      <Dialog onOpenChange={onOpenChange} open>
        <DialogContent>
          <DialogTitle>Test dialog</DialogTitle>
          <Select defaultOpen={initiallyOpen}>
            <SelectTrigger aria-label="picker">
              <SelectValue placeholder="pick" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="a">Option A</SelectItem>
            </SelectContent>
          </Select>
        </DialogContent>
      </Dialog>
    )

    // DismissableLayer registers its document pointerdown listener in a
    // setTimeout(0) — flush it first.
    await flushTimers()

    if (!initiallyOpen) {
      fireEvent.keyDown(screen.getByRole('combobox', { name: 'picker' }), { key: 'ArrowDown' })
    }

    await flushTimers()

    const overlay = window.document.querySelector('[data-slot="dialog-overlay"]') as HTMLElement
    expect(overlay).toBeTruthy()
    expect(screen.getByText('Option A')).toBeTruthy()

    fireEvent.pointerDown(overlay, { button: 0 })
    fireEvent.pointerUp(overlay, { button: 0 })
    fireEvent.click(overlay, { button: 0 })

    // The deferred dismissal path resolves through another setTimeout(0).
    await flushTimers()

    // The select closed...
    expect(screen.queryByText('Option A')).toBeNull()
    // ...but the dialog must NOT have been asked to close.
    expect(onOpenChange).not.toHaveBeenCalledWith(false)

    // Ownership applies to this press only; the next press may close the dialog.
    fireEvent.pointerDown(overlay, { button: 0 })
    fireEvent.pointerUp(overlay, { button: 0 })
    fireEvent.click(overlay, { button: 0 })
    await flushTimers()
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('still closes the dialog on a genuine overlay click when no dropdown is open', async () => {
    const onOpenChange = vi.fn()
    render(
      <Dialog onOpenChange={onOpenChange} open>
        <DialogContent>
          <DialogTitle>Test dialog</DialogTitle>
          <Select>
            <SelectTrigger aria-label="picker">
              <SelectValue placeholder="pick" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="a">Option A</SelectItem>
            </SelectContent>
          </Select>
        </DialogContent>
      </Dialog>
    )

    await flushTimers()

    const overlay = window.document.querySelector('[data-slot="dialog-overlay"]') as HTMLElement
    fireEvent.pointerDown(overlay, { button: 0 })
    fireEvent.pointerUp(overlay, { button: 0 })
    fireEvent.click(overlay, { button: 0 })
    await flushTimers()

    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
