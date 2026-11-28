import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * An iPhone photo (HEIC) attaches from the composer. Some browsers send it with
 * an empty type, so the composer decides by its name and sends it with the
 * images; the engine converts it to JPEG, or, on a host that cannot, attaches
 * it as a file and says so.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
// A real 400x200 HEIC (EXIF orientation 6) made with macOS ImageIO; the same
// bytes as tests/fixtures/heic/orientation-6.heic.
const HEIC = Buffer.from(
  'AAAAJGZ0eXBoZWljAAAAAG1pZjFNaVBybWlhZk1pSEJoZWljAAABwm1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAHBpY3QAAAAAAAAAAAAAAAAAAAAAJGRpbmYAAAAcZHJlZgAAAAAAAAABAAAADHVybCAAAAABAAAADnBpdG0AAAAAAAEAAAA4aWluZgAAAAAAAgAAABVpbmZlAgAAAAABAABodmMxAAAAABVpbmZlAgAAAQACAABFeGlmAAAAABppcmVmAAAAAAAAAA5jZHNjAAIAAQABAAAA5WlwcnAAAADEaXBjbwAAABNjb2xybmNseAACAAIABoAAAAAMY2xsaQDLAEAAAAAUaXNwZQAAAAAAAAGQAAAAyAAAAAlpcm90AwAAABBwaXhpAAAAAAMICAgAAABwaHZjQwEDcAAAALAAAAAAADzwAPz9+PgAAAsDoAABABdAAQwB//8DcAAAAwCwAAADAAADADxwJKEAAQAiQgEBA3AAAAMAsAAAAwAAAwA8oAyIDR8uIe5FlU3AgIGAIKIAAQAJRAHAYXLIQFMkAAAAGWlwbWEAAAAAAAAAAQABBoECAwWGhAAAACxpbG9jAAAAAEQAAAIAAQAAAAEAAAIaAAAAyQACAAAAAQAAAfYAAAAkAAAAAW1kYXQAAAAAAAAA/QAAAAZFeGlmAABNTQAqAAAACAABARIAAwAAAAEABgAAAAAAAAAAAMUoAa+c6wcMnihMRoF8xfqpX//tlP//xNYPsW/Z6Ue6Guho//+IHyNHWfowcvaViUEygAAAAwPKPm3F/OOKi/2H53wIABAwIrFiI+1ipNDUAAARUGfD8PZ5M7f96XnPEXDxMAz7Ca7TUPgvgEUFf/IppdZiZa+JgAAMGpqSpjc/c7UIKjkXG1NfeowTTkivhlvR10Csvd18hk/BZx5YL5igAAmIUyVneeTtQD4AAXEAAAMAAVtMADrgAAADAAADAAfaAAByQA==',
  'base64',
)
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)
const DROP_REF = `drop_${'f'.repeat(32)}`

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `composer-heic-${testInfo.workerIndex}`)
})

test('a HEIC photo with an empty type uploads as an image', async ({ page }) => {
  const uploads: string[] = []
  await page.route(`**/api/chats/${CHAT_ID}/attachments*`, route => {
    uploads.push('attachments')
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{"file_refs":[],"errors":[]}' })
  })
  await page.route(`**/api/chats/${CHAT_ID}/images`, route => {
    uploads.push('images')
    expect(route.request().postDataBuffer()?.includes(Buffer.from('filename="IMG_0001.HEIC"'))).toBe(true)
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{ ref: 'web_heic.jpg', mime_type: 'image/jpeg', filename: 'IMG_0001.HEIC' }]),
    })
  })
  await page.route('**/api/images/web_heic.jpg', route => route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1X1 }))
  await boot(page, CHAT_PATH, COMPOSER)

  await page.locator('.image-btn input[type=file]').setInputFiles({ name: 'IMG_0001.HEIC', mimeType: '', buffer: HEIC })

  await expect(page.locator('.image-preview')).toHaveCount(1)
  await expect(page.locator(COMPOSER)).toHaveValue('[Image 1] ')
  expect(uploads).toEqual(['images'])
})

test('a HEIC photo the engine cannot convert is attached as a file, with a notice', async ({ page }, testInfo) => {
  await page.route(`**/api/chats/${CHAT_ID}/images`, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{
      filename: 'IMG_0002.HEIC',
      file_ref: { ref: DROP_REF, name: 'IMG_0002.HEIC' },
      notice: 'The agent can read it by path but cannot see it as an image: this computer has no HEIC converter (only macOS has one built in).',
    }]),
  }))
  await boot(page, CHAT_PATH, COMPOSER)

  await page.locator('.image-btn input[type=file]').setInputFiles({ name: 'IMG_0002.HEIC', mimeType: 'image/heic', buffer: HEIC })

  await expect(page.locator('.image-preview')).toHaveCount(0)
  await expect(page.locator('.pending-attachments')).toContainText('IMG_0002.HEIC')
  const toast = page.locator('.toast').filter({ hasText: 'IMG_0002.HEIC attached as a file' })
  await expect(toast).toBeVisible()
  await expect(toast).toHaveCSS('opacity', '1')
  await expect(toast).toContainText('cannot see it as an image')
  await page.screenshot({ path: testInfo.outputPath('heic-file-notice.png') })
})
