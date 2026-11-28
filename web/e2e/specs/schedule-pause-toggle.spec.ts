import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * The pause toggle of an automation is reachable exactly once at every width:
 * inline on a wide pane, in the ⋯ menu on a narrow pane (even in a wide window,
 * where the menu is portalled outside the pane) and on a phone.
 */

async function reachable(page: Page, name: string): Promise<{ inline: number; menu: number; menuHeight: number }> {
  const header = page.locator('.header-actions')
  const inline = await header.getByRole('button', { name, exact: true }).count()
  await page.getByRole('button', { name: 'More automation actions' }).click()
  const item = page.getByRole('menuitem', { name, exact: true })
  const menu = await item.count()
  const menuHeight = menu ? (await item.boundingBox())!.height : 0
  await page.keyboard.press('Escape')
  return { inline, menu, menuHeight }
}

test.describe('automation pause toggle', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `schedule-pause-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  test('wide pane: inline button only', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await boot(page, '/schedules/alpha-daily-brief', '.header-actions')
    const r = await reachable(page, 'Pause')
    expect(r.inline).toBe(1)
    expect(r.menu).toBe(0)
  })

  test('narrow pane in a wide window: menu item only, 44px', async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 900 })
    await boot(page, '/schedules/alpha-daily-brief', '.header-actions')
    const r = await reachable(page, 'Pause')
    expect(r.inline).toBe(0)
    expect(r.menu).toBe(1)
    expect(r.menuHeight).toBeGreaterThanOrEqual(44)
  })

  test.describe('phone', () => {
    test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
    test('menu item only, 44px', async ({ page }) => {
      await boot(page, '/schedules/alpha-daily-brief', '.header-actions')
      const r = await reachable(page, 'Pause')
      expect(r.inline).toBe(0)
      expect(r.menu).toBe(1)
      expect(r.menuHeight).toBeGreaterThanOrEqual(44)
    })
  })
})
