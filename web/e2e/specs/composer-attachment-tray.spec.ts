import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Staged images and comments share one chip shape, and at least two chips fit
 * on a row: a long comment cannot take a row to itself. An upload writes its
 * [Image N] into the prompt, and a chip opens the image in the viewer.
 */
const CHAT_ID = 'alpha-chat-1'
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)

async function stageComment(page: Page, note: string) {
  const dialog = page.getByRole('dialog', { name: 'Add comment', exact: true })
  // The trigger re-renders as the selection settles, so select and click as one retried step.
  await expect(async () => {
    await page.locator('.message .message-content').first().evaluate(el => {
      const range = document.createRange()
      range.selectNodeContents(el)
      window.getSelection()?.removeAllRanges()
      window.getSelection()?.addRange(range)
      document.dispatchEvent(new Event('selectionchange'))
    })
    await page.getByTitle('Comment on this selection', { exact: true }).click({ timeout: 2_000 })
    await expect(dialog).toBeVisible({ timeout: 1_000 })
  }).toPass({ timeout: 15_000 })
  await dialog.locator('textarea').fill(note)
  await dialog.getByRole('button', { name: 'Add comment', exact: true }).click()
  await expect(dialog).toBeHidden()
}

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 700 }]) {
  test(`staged chips share one shape and two fit per row at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await isolate(page, `composer-tray-${viewport.width}-${testInfo.workerIndex}`)
    let n = 0
    await page.route(`**/api/chats/${CHAT_ID}/images`, route => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify([{ ref: `img_tray_${++n}` }]),
    }))
    await page.route('**/api/images/img_tray_*', route => route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1X1 }))
    await boot(page, `/chat/${CHAT_ID}`, COMPOSER)
    await page.request.post('/__fixture__/transcript', { data: { shape: 'task-link' } })
    await page.reload()

    for (const name of ['a.png', 'b.png']) {
      await page.locator('.image-btn input[type=file]').setInputFiles({ name, mimeType: 'image/png', buffer: PNG_1X1 })
      await expect(page.locator('.composer-upload')).toHaveCount(0)
    }
    await stageComment(page, 'A long comment that would otherwise claim the whole row for itself and then some more')
    await stageComment(page, 'short')

    const chips = page.locator('.pending-attachments > *')
    await expect(chips).toHaveCount(4)
    const boxes = await chips.evaluateAll(els => els.map(el => el.getBoundingClientRect()).map(r => ({ top: Math.round(r.top), height: Math.round(r.height) })))
    // One chip height for images and comments alike.
    expect(new Set(boxes.map(b => b.height)).size).toBe(1)
    // At most two rows for four chips: every row holds at least two.
    expect(new Set(boxes.map(b => b.top)).size).toBeLessThanOrEqual(2)
    // The remove control stays inside its chip (nothing clipped by the tray).
    for (const chip of await chips.all()) {
      const [c, r] = await Promise.all([chip.boundingBox(), chip.locator('.comment-chip-remove').boundingBox()])
      expect(r!.y).toBeGreaterThanOrEqual(c!.y)
      expect(r!.x + r!.width).toBeLessThanOrEqual(c!.x + c!.width + 0.5)
    }
    // Each upload wrote its reference into the prompt.
    await expect(page.locator(COMPOSER)).toHaveValue('[Image 1] [Image 2] ')
    // Clicking an image chip opens it in the viewer.
    await page.locator('.image-ref-chip').first().click()
    const viewer = page.locator('.fv-modal')
    await expect(viewer.locator('.fv-img')).toHaveAttribute('src', '/api/images/img_tray_1')
    await page.keyboard.press('Escape')
    await expect(viewer).toHaveCount(0)
    // Removing image 1 drops its reference and renumbers image 2.
    await page.locator('.image-preview-remove').first().click()
    await expect(page.locator(COMPOSER)).toHaveValue('[Image 1] ')
    await page.locator('.composer-surface').screenshot({ path: testInfo.outputPath(`tray-${viewport.width}.png`) })
  })
}
