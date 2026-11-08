import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * A message attachment is a served `/api/images/<ref>`, not a workspace path.
 * Tapping it must open the in-app viewer over the chat, not navigate the whole
 * PWA to the raw image (an installed PWA replaces the transcript, and Back
 * returns to a reloaded chat).
 */
test('a message image opens in the file viewer', async ({ page }, testInfo) => {
  await isolate(page, `chat-image-${testInfo.workerIndex}`)
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  // The fixture chat starts empty; the image message needs the transcript.
  await page.request.post('/__fixture__/transcript', { data: {} })
  await page.reload()
  await expect(page.locator('.startup-overlay')).toHaveCount(0)

  const link = page.locator('.message-image-link')
  await expect(link).toBeVisible()
  await expect(link.locator('.message-image')).toHaveAttribute('src', '/api/images/upload-sample.png')

  const url = page.url()
  await link.click()

  const viewer = page.locator('.fv-modal')
  await expect(viewer).toBeVisible()
  expect(page.url()).toBe(url)
  await expect(viewer.locator('.fv-title')).toHaveText('upload-sample.png')
  await expect(viewer.locator('.fv-img')).toHaveAttribute('src', '/api/images/upload-sample.png')

  await page.keyboard.press('Escape')
  await expect(viewer).toHaveCount(0)
  await expect(link).toBeFocused()
  expect(page.url()).toBe(url)
})
