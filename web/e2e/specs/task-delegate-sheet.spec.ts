import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Why this is a browser test: the sheet's width and whether the model chip stays
 * on one line while its list is open are layout facts jsdom does not compute. The
 * chip once wrapped onto four lines beside the open list, and only a screenshot
 * showed it.
 */
test.describe('the delegate sheet', () => {
  test.use({ viewport: { width: 1600, height: 950 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `delegate-sheet-${testInfo.workerIndex}-${testInfo.testId}`)
  })

  test('is as wide as the task editor and keeps the model chip on one line', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'task-link' }),
    }))
    await page.reload()
    await page.waitForSelector(COMPOSER)
    await page.locator('.message-wrap.assistant a', { hasText: 'Reply to Ivo' }).click()
    await page.locator('.task-delegate-actions button', { hasText: 'Delegate to agent' }).click()

    const sheet = page.locator('.task-delegate-sheet')
    await expect(sheet).toBeVisible()
    // Far wider than the 520px default modal sheet it used to be.
    expect((await sheet.boundingBox())!.width).toBeGreaterThan(1000)

    const chip = sheet.locator('.task-delegate-model')
    const closedHeight = (await chip.boundingBox())!.height
    await chip.click()
    await expect(page.getByRole('option').first()).toBeVisible()
    expect((await chip.boundingBox())!.height).toBe(closedHeight)

    // The first Escape closes the list and hands focus back; the sheet stays.
    await page.keyboard.press('Escape')
    await expect(page.getByRole('option')).toHaveCount(0)
    await expect(sheet).toBeVisible()
    await expect(chip).toBeFocused()
  })
})
