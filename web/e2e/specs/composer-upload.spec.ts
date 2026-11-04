import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * A composer upload shows a labelled bar, and Send waits for it. The bar and
 * the disabled Send are laid out and wired by the browser, so jsdom cannot
 * prove them: the upload is held open here and released by the test.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `composer-upload-${testInfo.workerIndex}`)
})

test('an image upload shows a loading bar and holds Send until it lands', async ({ page }) => {
  let release!: () => void
  const released = new Promise<void>((resolve) => { release = resolve })
  await page.route(`**/api/chats/${CHAT_ID}/images`, async (route) => {
    await released
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[{"ref":"img_e2e"}]' })
  })
  await page.route('**/api/images/img_e2e', route => route.fulfill({
    status: 200, contentType: 'image/png', body: PNG_1X1,
  }))
  await boot(page, CHAT_PATH, COMPOSER)

  await page.locator('.image-btn input[type=file]').setInputFiles({
    name: 'shot.png', mimeType: 'image/png', buffer: PNG_1X1,
  })

  const upload = page.locator('.composer-upload')
  await expect(upload).toBeVisible()
  await expect(upload).toContainText('shot.png')
  // The route holds the response, so the bar may already read "Processing".
  await expect(page.locator('.composer-upload-label')).toHaveText(/Loading shot\.png…|Processing shot\.png…/)

  await page.locator(COMPOSER).fill('hi')
  await expect(page.locator('.send-btn')).toBeDisabled()

  release()
  await expect(page.locator('.composer-upload')).toHaveCount(0)
  await expect(page.locator('.image-preview')).toHaveCount(1)
  await expect(page.locator('.send-btn')).toBeEnabled()
})
