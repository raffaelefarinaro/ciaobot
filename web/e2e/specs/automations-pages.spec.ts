import { expect, test } from '@playwright/test'
import { boot, horizontalOverflow, isolate, quiesce } from '../support/app'

/**
 * The Automations destination is three pages: Schedules (/schedules), Loops
 * (/automations/loops) and Webhooks (/automations/webhooks). On a desktop the
 * sidebar lists them as destination rows over Next up; on a phone the sidebar
 * is a drawer, so each page starts with a three-segment switcher instead.
 */

test.describe('automations pages, desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `automations-desktop-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  test('the sidebar moves between the three pages as real links, the current one marked', async ({ page }) => {
    await boot(page, '/schedules', '.overview-body a.ov-item')
    const subnav = page.getByRole('navigation', { name: 'Automations', exact: true })
    const current = subnav.locator('[aria-current="page"]')
    await expect(current).toHaveAttribute('href', '/schedules')
    // A destination row: the primary nav's 38px on a fine pointer, its accent fill.
    expect((await current.boundingBox())!.height).toBe(38)
    // The expanded sidebar names the pages, so the switcher is not drawn.
    await expect(page.locator('.automations-switcher')).toBeHidden()
    await expect(page.locator('.page-rail .docked-window')).toContainText('Recent runs')

    await subnav.getByRole('link', { name: /^Loops/ }).click()
    await expect(page).toHaveURL(/\/automations\/loops$/)
    await expect(page.locator('#ov-loops-title')).toBeFocused()
    await expect(subnav.locator('[aria-current="page"]')).toHaveAttribute('href', '/automations/loops')

    // Keyboard: the rows are links in tab order, and Enter follows one.
    await subnav.getByRole('link', { name: /^Webhooks/ }).focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/automations\/webhooks$/)
    await expect(page.locator('#wh-section-title')).toBeVisible()
  })

  test('Next up lists the soonest runs and opens the automation', async ({ page }) => {
    await boot(page, '/schedules', '.automations-next-item')
    const rows = page.locator('.automations-next-item')
    await expect(rows).toHaveText([/Daily project briefing/, /Weekly review/, /Inbox sweep/])
    await rows.first().click()
    await expect(page).toHaveURL(/\/schedules\/alpha-daily-brief$/)
    await expect(rows.first()).toHaveAttribute('aria-current', 'page')
  })

  test('a collapsed sidebar brings the switcher back', async ({ page }) => {
    await boot(page, '/schedules', '.overview-body a.ov-item')
    await page.getByRole('button', { name: 'Collapse sidebar' }).click()
    await expect(page.locator('.automations-switcher')).toBeVisible()
  })
})

test.describe('automations pages, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `automations-phone-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  for (const path of ['/schedules', '/automations/loops', '/automations/webhooks']) {
    test(`${path} starts with the 44px switcher and never scrolls sideways`, async ({ page }) => {
      await boot(page, path, '.automations-switcher')
      const segments = page.locator('.automations-switcher a')
      await expect(segments).toHaveText(['Schedules', 'Loops', 'Webhooks'])
      await expect(page.locator('.automations-switcher [aria-current="page"]')).toHaveAttribute('href', path)
      await quiesce(page)
      for (const segment of await segments.all()) {
        expect((await segment.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      }
      const overflow = await horizontalOverflow(page)
      expect(overflow.culprits).toEqual([])
      expect(overflow.overflow).toBeLessThanOrEqual(0)
    })
  }

  test('a segment keeps focus when it switches the page', async ({ page }) => {
    await boot(page, '/schedules', '.automations-switcher')
    const loops = page.locator('.automations-switcher a[href="/automations/loops"]')
    await loops.focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/automations\/loops$/)
    await expect(loops).toBeFocused()
    await expect(loops).toHaveAttribute('aria-current', 'page')
  })

  test('the drawer holds the pages and Next up', async ({ page }) => {
    await boot(page, '/schedules', '.automations-switcher')
    await page.getByRole('button', { name: 'Open sidebar' }).click()
    const subnav = page.getByRole('navigation', { name: 'Automations', exact: true })
    await expect(subnav.getByRole('link')).toHaveCount(3)
    for (const link of await subnav.getByRole('link').all()) {
      expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    }
    await expect(page.locator('.automations-next-item')).toHaveCount(3)
    // Following a page closes the drawer, so focus lands on the page, not
    // behind the drawer.
    await subnav.getByRole('link', { name: /^Loops/ }).click()
    await expect(page).toHaveURL(/\/automations\/loops$/)
    await expect(subnav).toBeHidden()
    await expect(page.locator('#ov-loops-title')).toBeFocused()
  })
})
