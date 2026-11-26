import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Memory → Unsure is a page over one Markdown file, so what only a real layout
 * can say is how it sits: the rows wrap long lines instead of scrolling the
 * page sideways, and its two text actions meet the 44px touch floor on a phone.
 * The file is answered here rather than added to the shared fixture, because
 * the list's contents are this spec's business alone.
 */
const LIST = [
  '# Unsure',
  '',
  '- [ ] memory: prefers short briefs — unsure because it was said once; 2026-11-01',
  '- [x] people: Ada is a client — source: chat-1; 2026-11-02',
  '- [ ] project: ' + 'a very long unbroken line of text '.repeat(8) + ' — source: chat-9; 2026-11-03',
  '',
].join('\n')

async function answerList(page: Page, body: string | null) {
  await page.route(/\/api\/workspace-file\?.*Unsure\.md/, route => (
    body === null
      ? route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"not found"}' })
      : route.fulfill({ status: 200, contentType: 'text/plain', body })
  ))
}

test.describe('Memory › Unsure', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `memory-unsure-${testInfo.workerIndex}`)
  })

  test('lists the unticked lines and names the file', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await answerList(page, LIST)
    await boot(page, '/memory/unsure', '.mu-item')

    await expect(page.locator('.page-tag')).toContainText('Unsure')
    await expect(page.locator('.mu-count')).toHaveText('2 items waiting.')
    await expect(page.locator('.mu-item')).toHaveCount(2)
    await expect(page.locator('.mu-item').first()).toContainText('prefers short briefs')
    await expect(page.getByRole('button', { name: 'Open Unsure.md' })).toBeEnabled()
    await page.screenshot({ path: 'test-results/memory-unsure-desktop.png' })
  })

  test('says so when there is no list yet', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await answerList(page, null)
    await boot(page, '/memory/unsure', '.mu-lede')

    await expect(page.locator('.mu-note')).toContainText('Nothing is on the list for this workspace.')
    await expect(page.getByRole('button', { name: 'Open Unsure.md' })).toBeDisabled()
  })
})

test.describe('Memory › Unsure on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `memory-unsure-phone-${testInfo.workerIndex}`)
  })

  test('does not scroll sideways and keeps both actions 44px tall', async ({ page }) => {
    await answerList(page, LIST)
    await boot(page, '/memory/unsure', '.mu-item')

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `scrolls ${overflow}px sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    for (const name of ['Open Unsure.md', 'Go through it with Ciaobot']) {
      const box = await page.getByRole('button', { name }).boundingBox()
      expect(box, name).not.toBeNull()
      expect(box!.height, name).toBeGreaterThanOrEqual(44)
    }
    await page.screenshot({ path: 'test-results/memory-unsure-phone.png' })
  })
})
