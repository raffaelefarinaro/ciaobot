import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * The first message of a delegated chat is a "Task handed over" card. The
 * instructions the user typed into the delegation sheet are drawn in it, above the
 * disclosure, instead of sitting far down the raw prompt. Wrapping and overflow
 * are layout facts jsdom does not compute, so this is checked in a browser at
 * desktop and phone widths.
 */
const SIZES = [
  { width: 1280, height: 900, phone: false },
  { width: 390, height: 844, phone: true },
]
for (const viewport of SIZES) {
  test.describe(`at ${viewport.width}px`, () => {
  test.use({ viewport, isMobile: viewport.phone, hasTouch: viewport.phone })
  test(`shows the user's instructions in the card`, async ({ page }, testInfo) => {
    await isolate(page, `task-handover-${testInfo.workerIndex}-${viewport.width}`)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'task-handover' }),
    }))
    await page.reload()

    const card = page.locator('section[aria-label="Task handed over"]')
    await expect(card).toHaveCount(1)
    const block = card.getByRole('group', { name: 'Your instructions' })
    await expect(block).toContainText('Write it in Spanish, warm but short.')
    await expect(block.locator('strong')).toHaveText('not')
    await expect(block).toContainText('Never write </delegation-instructions> in the draft')

    // Above the disclosure, and the disclosure still closed.
    const blockBox = (await block.boundingBox())!
    const summary = card.locator('summary')
    const summaryBox = (await summary.boundingBox())!
    expect(blockBox.y + blockBox.height).toBeLessThanOrEqual(summaryBox.y)
    await expect(card.locator('details')).not.toHaveAttribute('open', '')
    const cardBox = (await card.boundingBox())!
    expect(cardBox.x + cardBox.width).toBeLessThanOrEqual(viewport.width)
    if (viewport.phone) expect(summaryBox.height).toBeGreaterThanOrEqual(43.5)
    await card.screenshot({ path: testInfo.outputPath(`task-handover-${viewport.width}.png`) })

    // The raw prompt is unchanged: the instructions are still in it, in their fence.
    await summary.click()
    const raw = card.getByRole('region', { name: 'Prompt the agent received' })
    await expect(raw).toContainText('<delegation-instructions>')
    await expect(raw).toContainText('Write it in Spanish, warm but short.')
    await raw.focus()
    await expect(raw).toBeFocused()
    await card.screenshot({ path: testInfo.outputPath(`task-handover-open-${viewport.width}.png`) })
  })
  test('draws no instructions block when none were typed', async ({ page }, testInfo) => {
    await isolate(page, `task-handover-plain-${testInfo.workerIndex}-${viewport.width}`)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'task-handover-plain' }),
    }))
    await page.reload()
    const card = page.locator('section[aria-label="Task handed over"]')
    await expect(card).toContainText('Reply to the Xmas 2026 email from Iciar.')
    await expect(card.getByRole('group', { name: 'Your instructions' })).toHaveCount(0)
    await card.screenshot({ path: testInfo.outputPath(`task-handover-plain-${viewport.width}.png`) })
  })
  })
}
