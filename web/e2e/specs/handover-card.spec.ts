import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * A provider handover is drawn as the injected-turn notice card, collapsed,
 * with the exact pack the new agent received one disclosure away (#2397).
 * Checked on a phone, where the header's facts must stay inside the card.
 */
test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

test.describe('handover card', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `handover-card-${testInfo.workerIndex}`)
  })

  test('draws the marker as a collapsed card that reveals what the agent received', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'handover' }),
    }))
    await page.reload()

    const card = page.locator('.injected-card--handover')
    await expect(card).toHaveCount(1)
    await expect(card.locator('.injected-card-kind')).toHaveText('Handed over')
    await expect(page.locator('.message.system', { hasText: 'Handed over from' })).toHaveCount(0)
    const toggle = card.locator('.injected-card-toggle')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    const cardBox = (await card.boundingBox())!
    const toggleBox = (await toggle.boundingBox())!
    expect(toggleBox.height).toBeGreaterThanOrEqual(44)
    expect(toggleBox.x + toggleBox.width).toBeLessThanOrEqual(cardBox.x + cardBox.width + 0.5)
    await card.screenshot({ path: test.info().outputPath('handover-collapsed.png') })

    await toggle.click()
    await card.getByText('Show what the agent received').click()
    const region = card.getByRole('region', { name: 'What the agent received' })
    await expect(region).toContainText('<provider-handover>')
    await expect(region).toContainText('Assistant: The native wizard worker hit the usage limit mid-change.')
    await card.screenshot({ path: test.info().outputPath('handover-open.png') })
  })
})
