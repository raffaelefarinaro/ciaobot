import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Why this is a browser test: the turn footer hides until hover, and the vault
 * check must not. Only a real render shows the badge visible at rest and its
 * claim list opening in place (#2874).
 */
test.describe('vault answer check badge', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `vault-check-${testInfo.workerIndex}`)
  })

  for (const width of [1280, 390]) {
    test(`is visible without hover and lists the claims at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      await page.evaluate(() => fetch('/__fixture__/transcript', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ shape: 'vault-check' }),
      }))
      await page.reload()
      await page.waitForSelector(COMPOSER)

      const toggle = page.getByRole('button', { name: /Vault check: 1 of 2 claims supported/ })
      await expect(toggle).toBeVisible()
      await expect(toggle).toHaveText(/Vault 1\/2/)
      await expect(toggle).toHaveAttribute('aria-expanded', 'false')

      await toggle.focus()
      await page.keyboard.press('Enter')
      await expect(toggle).toHaveAttribute('aria-expanded', 'true')
      const list = page.locator('.vault-check-list')
      await expect(list).toContainText('Supported')
      await expect(list).toContainText('People/Ann.md')
      await expect(list).toContainText('Not in your notes')
      await expect(list).toContainText('General knowledge')
      if (process.env.VAULT_CHECK_SHOTS) {
        await page.locator('.message-wrap.assistant').last().screenshot({ path: `${process.env.VAULT_CHECK_SHOTS}/vault-check-${width}.png` })
      }
    })
  }
})
