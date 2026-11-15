import { expect, test } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

test.describe('retired vault answer check UI', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `vault-ui-removal-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  for (const width of [1280, 390]) {
    test(`keeps an ordinary answer and Activity but omits historical verdict UI at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 })
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      await page.evaluate(() => fetch('/__fixture__/transcript', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ shape: 'vault-check' }),
      }))
      await page.reload()
      await page.waitForSelector(COMPOSER)

      const answer = page.locator('.message-wrap.assistant').last()
      await expect(answer).toContainText('Ann moved to Rome in March')
      await expect(answer.locator('.vault-check-toggle, .vault-check-list')).toHaveCount(0)
      await expect(page.locator('.vault-check')).toHaveCount(0)
      const activity = page.locator('.trace-summary').first()
      await expect(activity).toBeVisible()
      await activity.click()
      await expect(page.locator('.activity-line')).toContainText('$ Bash git status')
      if (width === 1280) {
        await expect(answer.locator('.message-meta')).toBeVisible()
      }
      await page.screenshot({ path: testInfo.outputPath(`chat-${width}.png`), fullPage: true })
    })

    test(`Settings has no vault-check card at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 })
      await boot(page, '/settings', '.settings-main')
      await expect(page.locator('.settings-main')).toBeVisible()
      await expect(page.getByText(/check vault answers/i)).toHaveCount(0)
      await expect(page.locator('.settings-main').getByText(/vault answer check/i)).toHaveCount(0)
      await page.screenshot({ path: testInfo.outputPath(`settings-${width}.png`), fullPage: true })
      if (width === 1280) {
        const keyboardSection = page.locator('.settings-toc-item').filter({ hasText: 'Keyboard shortcuts' })
        await expect(keyboardSection).toBeVisible()
        await keyboardSection.focus()
        await page.keyboard.press('Enter')
        await expect(page.locator('#settings-keyboard-shortcuts')).toBeFocused()

        // As the zoom suite documents, halving the CSS viewport models 200%
        // browser zoom; check the neighboring Settings content still reflows.
        await page.setViewportSize({ width: 640, height: 450 })
        await expect(page.locator('.settings-main')).toBeVisible()
        await expect(page.getByText(/check vault answers/i)).toHaveCount(0)
        const { overflow } = await horizontalOverflow(page)
        expect(overflow).toBeLessThanOrEqual(0)
      }
    })
  }
})
