import { expect, test } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The rail's visibility is decided by a container query on the chat pane, and
 * jsdom applies no CSS and has no layout, so only a real engine can tell
 * whether the On this page rail is beside the page or gone at a given window
 * width.
 */
const SETTINGS = '.settings-main'
const DESKTOP_1280 = { width: 1280, height: 800 }

test.describe('settings on-this-page rail', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `settings-rail-${testInfo.workerIndex}`)
  })

  test('keeps the on-this-page rail beside Settings at a 1280px window', async ({ page }) => {
    await page.setViewportSize(DESKTOP_1280)
    await boot(page, '/settings', SETTINGS)

    const rail = page.locator('.settings-rail')
    await expect(rail).toBeVisible()
    const box = await rail.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThanOrEqual(180)
    expect(box!.x + box!.width).toBeLessThanOrEqual(DESKTOP_1280.width)

    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)
  })

  test('keeps it with the widest sidebar', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('ciao:sidebar-width', '500'))
    await page.setViewportSize(DESKTOP_1280)
    await boot(page, '/settings', SETTINGS)

    const rail = page.locator('.settings-rail')
    await expect(rail).toBeVisible()
    const box = await rail.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThanOrEqual(180)
    expect(box!.x + box!.width).toBeLessThanOrEqual(DESKTOP_1280.width)

    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)
  })

  test('drops it on a narrow pane', async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 800 })
    await boot(page, '/settings', SETTINGS)

    await expect(page.locator('.settings-rail')).toBeHidden()
  })
})
