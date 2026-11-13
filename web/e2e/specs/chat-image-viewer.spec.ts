import { readFileSync } from 'node:fs'
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
  await expect(link).toHaveText('Image 1')

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

/**
 * An assistant message that embeds a workspace image by relative path must
 * show it. Left as written the src resolved against the /chat/... route and
 * loaded the app shell: a broken-image box on the phone.
 */
test('an embedded workspace image loads through the chat-scoped image route', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await isolate(page, `chat-embedded-image-${testInfo.workerIndex}`)
  const shot = 'memory-vault/projects/active/ciao-improvements/code-mode-auto-chat-details-v2.png'
  const png = readFileSync(new URL('../../public/icons/icon-192.png', import.meta.url))
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'user', content: 'Show the screenshot', turn_index: 0 },
    { role: 'assistant', content: `Here it is:\n\n![Chat details after the change](${shot})`, turn_index: 0 },
  ] }))
  const imageRequests: string[] = []
  await page.route('**/api/workspace-image?*', route => {
    imageRequests.push(route.request().url())
    return route.fulfill({ body: png, contentType: 'image/png' })
  })

  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  const img = page.getByRole('img', { name: 'Chat details after the change' })
  await expect(img).toBeVisible()
  await expect.poll(() => img.evaluate(el => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  const url = new URL(imageRequests[0])
  expect(url.searchParams.get('path')).toBe(shot)
  expect(url.searchParams.get('chat_id')).toBe('alpha-chat-1')
})
