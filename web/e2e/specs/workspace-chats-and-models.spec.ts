import { expect, test } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Why this is a browser test: the Chats and models card mixes a segmented
 * radiogroup, native selects and popover pickers inside the container-query
 * Overview page. Only a real engine can say they fit at phone width and that
 * an edit surfaces the page's single Save changes row.
 */
const OVERVIEW = '/settings/workspace'
const READY = '.workspace-models .section-title'

test.describe('workspace chats and models', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `workspace-chats-and-models-${testInfo.workerIndex}`)
  })

  test('an edit shows Save changes and Discard clears it', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await boot(page, OVERVIEW, READY)

    await expect(page.locator('.workspace-save')).toHaveCount(0)
    const mode = page.locator('select[id^="ws-override-permission_mode-"]')
    await expect(mode.locator('option').first()).toContainText('Engine default')
    await mode.selectOption('manual')
    await expect(page.locator('.workspace-save')).toBeVisible()
    await page.locator('.workspace-discard').click()
    await expect(page.locator('.workspace-save')).toHaveCount(0)
    await expect(mode).toHaveValue('')
  })

  for (const [width, height] of [[390, 844], [640, 450]] as const) {
    test(`does not scroll sideways at ${width}x${height}`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await boot(page, OVERVIEW, READY)
      const { overflow, culprits } = await horizontalOverflow(page)
      expect(overflow, `scrolls ${overflow}px sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    })
  }
})
