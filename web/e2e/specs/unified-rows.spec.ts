import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * One row language across the app's lists: the Settings and Memory section
 * lists are sidebar rows like a chat's (the fill alone marks the open one),
 * the sidebar's lists load in that row's shape, Home's chats and the
 * Automations overview share one page-row component and one arrow-key roaming,
 * and Usage loads in the shared skeleton.
 */

test.describe('unified rows, desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `unified-rows-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  test('Settings and Memory sections are sidebar rows; the open one is filled, not bold', async ({ page }) => {
    await boot(page, '/settings/providers', '.settings-nav-list')
    const active = page.locator('.settings-nav-list .sidebar-row.active')
    await expect(active).toHaveCount(1)
    await expect(active).toHaveText('Providers')
    const idle = page.locator('.settings-nav-list .sidebar-row:not(.active)').first()
    const weight = (el: Element) => getComputedStyle(el.querySelector('.sidebar-row-title')!).fontWeight
    expect(await active.evaluate(weight)).toBe(await idle.evaluate(weight))
    expect(await active.evaluate(el => getComputedStyle(el).backgroundColor))
      .not.toBe(await idle.evaluate(el => getComputedStyle(el).backgroundColor))
    // The same box as an automation row: 7px radius, 36px on a fine pointer.
    expect(await active.evaluate(el => getComputedStyle(el).borderRadius)).toBe('7px')
    expect((await active.boundingBox())!.height).toBe(36)

    await page.goto('/memory/map')
    const current = page.locator('.memory-nav-list .sidebar-row[aria-current="page"]')
    await expect(current).toContainText('Map')
    expect(await current.evaluate(weight)).toBe('400')
  })

  test('the automation list loads as sidebar rows', async ({ page }) => {
    let release!: () => void
    const held = new Promise<void>(resolve => { release = resolve })
    await page.route('**/api/schedules**', async route => { await held; await route.continue() })
    await page.goto('/schedules')
    const skeleton = page.locator('.schedules-list .sidebar-skeleton[role="status"]')
    await expect(skeleton).toHaveText('Loading automations…')
    await expect(skeleton.locator('.sidebar-row--placeholder')).toHaveCount(3)
    // The placeholder is the row's box: same height as the rows that replace it.
    expect((await skeleton.locator('.sidebar-row--placeholder').first().boundingBox())!.height).toBe(36)
    release()
    await expect(page.locator('.schedules-list .schedule-item')).toHaveCount(3)
    await expect(skeleton).toHaveCount(0)
  })

  test('Home and the Automations overview share one row, and arrows roam both', async ({ page }) => {
    await boot(page, '/')
    const home = page.locator('.home-chat-item:not(.home-chat-item--insight)').first()
    await expect(home).toHaveClass(/list-row/)
    const homeBox = (await home.boundingBox())!

    await page.goto('/schedules')
    const rows = page.locator('.overview-body a.ov-item')
    await expect(rows).toHaveCount(3)
    await expect(rows.first()).toHaveClass(/list-row/)
    // The first overview row also draws the rule above it, so compare the second.
    expect((await rows.nth(1).boundingBox())!.height).toBe(homeBox.height)
    const end = (el: Element) => getComputedStyle(el.querySelector('.list-row-when')!).fontFamily
    expect(await rows.first().evaluate(end)).toBe(await page.locator('.schedules-list .sidebar-row-meta').first().evaluate(el => getComputedStyle(el).fontFamily))

    // Arrows from the page body roam the sidebar list first, as before; with
    // focus on an overview row they move along the overview.
    await rows.nth(0).focus()
    await page.keyboard.press('ArrowDown')
    await expect(rows.nth(1)).toBeFocused()
    await page.keyboard.press('ArrowRight')
    await expect(rows.nth(2)).toBeFocused()
    await page.keyboard.press('ArrowDown')
    await expect(rows.nth(2)).toBeFocused()
    await page.keyboard.press('ArrowUp')
    await expect(rows.nth(1)).toBeFocused()
  })

  test('Usage draws the shared skeleton on its first load', async ({ page }) => {
    let release!: () => void
    const held = new Promise<void>(resolve => { release = resolve })
    await page.route('**/api/stats**', async route => { await held; await route.continue() })
    await page.goto('/stats')
    await expect(page.locator('.usage-view .skeleton[role="status"]')).toHaveAttribute('aria-label', 'Loading usage')
    await expect(page.locator('.usage-kpi')).toHaveCount(0)
    release()
    await expect(page.locator('.usage-kpi')).toHaveCount(8)
    await expect(page.locator('.usage-view .skeleton')).toHaveCount(0)
  })
})
