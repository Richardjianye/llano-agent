import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'
import type { Locale } from '@/i18n/types'
import { resetSidebarView, setSidebarGrouping } from '@/store/layout'

import { SidebarFilterMenu } from './filter-menu'

// Real menu, real stores, real locale catalogs: the point is that what the
// user reads in the dropdown follows the app language.

beforeEach(() => {
  resetSidebarView()
  setSidebarGrouping('project')
})

afterEach(() => {
  cleanup()
  resetSidebarView()
})

async function openMenu(locale: Locale) {
  render(
    <I18nProvider configClient={null} initialLocale={locale}>
      <SidebarFilterMenu />
    </I18nProvider>
  )

  const trigger = screen.getByRole('button', { name: locale === 'zh' ? '筛选' : 'Filters' })

  // Radix opens on pointerdown, not on the bare click fireEvent dispatches.
  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)

  return screen.findByRole('menu')
}

async function openSubmenu(name: RegExp) {
  const sub = screen.getByRole('menuitem', { name })
  sub.focus()
  fireEvent.keyDown(sub, { key: 'ArrowRight' })

  const menus = await screen.findAllByRole('menu')

  return menus[menus.length - 1]
}

describe('SidebarFilterMenu', () => {
  it('reads in Chinese under the zh locale, current grouping included', async () => {
    const menu = await openMenu('zh')

    // The grouping row names its current value ("项目") beside its own label.
    expect(within(menu).getByRole('menuitem', { name: /分组\s*项目/ })).toBeTruthy()

    for (const label of ['排序', '显示', '状态', '配置档案', '全部标为已读']) {
      expect(within(menu).getByRole('menuitem', { name: new RegExp(label) })).toBeTruthy()
    }

    expect(within(menu).getByText('筛选')).toBeTruthy()
    expect(within(menu).getByRole('menuitemcheckbox', { name: '已归档' })).toBeTruthy()
    expect(within(menu).queryByText(/Grouping|Ordering|Mark all as read|Archived/)).toBeNull()
  })

  it('translates submenu options too', async () => {
    await openMenu('zh')

    const grouping = await openSubmenu(/分组/)

    expect(
      within(grouping)
        .getAllByRole('menuitemradio')
        .map(item => item.textContent)
    ).toEqual(['更新时间', '项目', '状态', '配置档案'])
  })

  it('keeps English under the en locale', async () => {
    const menu = await openMenu('en')

    expect(within(menu).getByRole('menuitem', { name: /Grouping\s*Project/ })).toBeTruthy()
    expect(within(menu).getByRole('menuitem', { name: /Mark all as read/ })).toBeTruthy()
    expect(within(menu).getByRole('menuitemcheckbox', { name: 'Archived' })).toBeTruthy()
  })
})
