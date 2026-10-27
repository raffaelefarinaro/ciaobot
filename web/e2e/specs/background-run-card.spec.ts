import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * A failed background run's collapsed card has to say why it failed on a
 * phone, where the header's facts are cut to an ellipsis. jsdom lays nothing
 * out, so the line's fit inside the card is checked in a real browser.
 */
test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

test.describe('background run card', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `background-run-card-${testInfo.workerIndex}`)
  })

  test('shows the failure line while collapsed, inside the card, without colour codes', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'background-run' }),
    }))
    await page.reload()

    const card = page.locator('.injected-card--background_run')
    const hint = card.locator('.injected-card-hint')
    await expect(hint).toHaveText("ModuleNotFoundError: No module named 'claude_agent_sdk'")

    const cardBox = (await card.boundingBox())!
    const hintBox = (await hint.boundingBox())!
    expect(hintBox.x).toBeGreaterThanOrEqual(cardBox.x)
    expect(hintBox.x + hintBox.width).toBeLessThanOrEqual(cardBox.x + cardBox.width)
    expect(hintBox.y + hintBox.height).toBeLessThanOrEqual(cardBox.y + cardBox.height)
    await card.screenshot({ path: test.info().outputPath('background-run-card.png') })

    await card.locator('.injected-card-toggle').click()
    await expect(hint).toBeHidden()
    await expect(card.locator('pre.injected-card-tail')).not.toContainText('\u001b')
  })
})
