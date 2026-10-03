import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Why this is a browser test: the fold is decided by a render heuristic over
 * phase-less history rows, and only a real render shows whether the reply is a
 * bubble or hidden inside the collapsed Activity trace (#630).
 */
test.describe('reply after a history replay', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `fold-${testInfo.workerIndex}`)
  })

  test('a short closing reply followed by reasoning renders as a bubble', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'opencode-fold' }),
    }))
    await page.reload()
    await page.waitForSelector(COMPOSER)

    const bubbles = page.locator('.message-wrap.assistant .message-row')
    await expect(bubbles.last()).toContainText('Good — updated both files.')
  })
})
