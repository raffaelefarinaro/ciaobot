import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

const TASK_ID = '0123456789abcdef0123456789abcdef'

/**
 * Why this is a browser test: whether a link opens a second tab, reloads the
 * page or routes in place is the browser's default action against the click
 * handler, and jsdom performs no navigation at all.
 */
test.describe('a task link in a chat reply', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `task-link-${testInfo.workerIndex}`)
  })

  test('opens the task on the board in place', async ({ page, context }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'task-link' }),
    }))
    await page.reload()
    await page.waitForSelector(COMPOSER)

    const link = page.locator('.message-wrap.assistant a', { hasText: 'Reply to Ivo' })
    await expect(link).toHaveAttribute('href', `/tasks/${TASK_ID}`)
    await expect(link).not.toHaveAttribute('target', /.*/)
    // A full page load would clear this marker; routing in place keeps it.
    await page.evaluate(() => { (window as unknown as { __kept: boolean }).__kept = true })

    await link.click()
    await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`))
    const sheet = page.locator('.task-sheet[role="dialog"]')
    await expect(sheet).toBeVisible()
    await expect(sheet.locator('#task-detail-name')).toHaveValue('Reply to Ivo in the Feedback Tracker')
    expect(await page.evaluate(() => (window as unknown as { __kept?: boolean }).__kept)).toBe(true)
    expect(context.pages()).toHaveLength(1)

    await page.keyboard.press('Escape')
    await expect(sheet).toBeHidden()
    await expect(page).toHaveURL(/\/tasks$/)
  })
})
