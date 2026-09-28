import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * Prototype A at a desktop width: layout facts jsdom cannot see. The review
 * rail must sit beside the request column rather than under it, and the
 * sidebar's stacked top (workspace, New chat, destinations) must not collapse
 * back into one crowded row - which is what it did at the default 340px
 * sidebar before the rail was restacked.
 */
test.use({ viewport: { width: 1440, height: 900 } })

test.describe('workbench layout', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `workbench-${testInfo.workerIndex}`)
  })

  test('the review rail sits beside the command surface', async ({ page }) => {
    await boot(page)
    const surface = await page.locator('.home-intake-form').boundingBox()
    const rail = await page.locator('.home-rail').boundingBox()
    expect(surface).not.toBeNull()
    expect(rail).not.toBeNull()
    expect(rail!.x).toBeGreaterThan(surface!.x + surface!.width)

    // Rows, not a card grid: each row spans the rail's width. Up-to-date
    // queues drop out, so the fixture shows proposals and automations only.
    const rows = page.locator('.home-review-item')
    await expect(rows).toHaveCount(2)
    for (const row of await rows.all()) {
      const box = await row.boundingBox()
      expect(box!.width).toBeGreaterThan(rail!.width - 2)
    }
  })

  test('the sidebar stacks workspace, New chat and navigation without overlap', async ({ page }) => {
    await boot(page)
    const order = ['.workspace-scope-trigger', '.sidebar-new-chat', '.nav-links']
    let previousBottom = -Infinity
    for (const selector of order) {
      const box = await page.locator(selector).first().boundingBox()
      expect(box, selector).not.toBeNull()
      expect(box!.y, `${selector} starts below the control above it`).toBeGreaterThanOrEqual(previousBottom - 0.5)
      previousBottom = box!.y + box!.height
    }
    // Every destination shows its label at the default sidebar width.
    for (const label of ['Home', 'Automations', 'Memory', 'Settings']) {
      await expect(page.locator('.nav-links .nav-item-label', { hasText: label })).toBeVisible()
    }
  })
})
