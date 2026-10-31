import { expect, test } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

test.use({ viewport: { width: 390, height: 500 }, isMobile: true, hasTouch: true })

test('comment action labels fit their buttons on a phone with the keyboard open', async ({ page }) => {
  await isolate(page, 'comment-actions-touch')
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await page.request.post('/__fixture__/transcript', { data: { shape: 'task-link' } })
  await page.reload()
  await page.locator('.message .message-content').first().evaluate(el => {
    const range = document.createRange()
    range.selectNodeContents(el)
    window.getSelection()?.removeAllRanges()
    window.getSelection()?.addRange(range)
    document.dispatchEvent(new Event('selectionchange'))
  })
  await page.getByTitle('Comment on this selection', { exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Add comment', exact: true })
  await dialog.locator('textarea').fill('A note')
  for (const name of ['Cancel', 'Add comment']) {
    const button = dialog.getByRole('button', { name, exact: true })
    await expect(button).toBeInViewport()
    const size = await button.evaluate(el => ({ width: el.clientWidth, contentWidth: el.scrollWidth, height: el.getBoundingClientRect().height }))
    expect(size.width).toBeGreaterThanOrEqual(size.contentWidth)
    expect(size.height).toBeGreaterThanOrEqual(44)
  }
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(dialog).toBeHidden()
})
