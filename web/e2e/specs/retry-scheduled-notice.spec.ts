import { expect, test, type Locator, type Page } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

async function setupRetry(page: Page, id: string, reason = 'manual') {
  await isolate(page, id)
  await page.route(/\/api\/chats(\/alpha-chat-1)?(\?.*)?$/, async route => {
    const response = await route.fetch()
    const body = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
    const withRetry = (chat: Record<string, unknown>) => chat.chat_id === 'alpha-chat-1'
      ? { ...chat, retry: { status: 'pending', next_at: '2099-01-01T00:00:00Z', last_error: reason, attempts: 0, interval_seconds: 3600 } }
      : chat
    await route.fulfill({ response, json: Array.isArray(body) ? body.map(withRetry) : withRetry(body) })
  })
}

async function tabTo(page: Page, target: Locator) {
  for (let i = 0; i < 100 && !(await target.evaluate(el => el === document.activeElement)); i++) {
    await page.keyboard.press('Tab')
  }
  await expect(target).toBeFocused()
}

for (const size of ['desktop', 'phone'] as const) {
  test.describe(size, () => {
    test.use(size === 'phone'
      ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true }
      : { viewport: { width: 1280, height: 900 } })

    test('standalone retry is neutral and its controls remain accessible', async ({ page }, testInfo) => {
      await setupRetry(page, `retry-scheduled-${testInfo.workerIndex}`)
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      const card = page.locator('.injected-card--retry_scheduled')
      await expect(card).toBeVisible()
      await expect(card).not.toContainText(/Unknown error|manual/)
      await expect(card).toContainText('Retrying this turn every hour')
      for (const name of ['Continue with...', 'Try now', 'Stop trying']) {
        const button = card.getByRole('button', { name, exact: true })
        await expect(button).toBeVisible()
        if (size === 'phone') {
          const box = (await button.boundingBox())!
          expect(box.width).toBeGreaterThanOrEqual(44)
          expect(box.height).toBeGreaterThanOrEqual(44)
        }
      }
      const toggle = card.getByRole('button', { name: 'Retry scheduled' })
      await tabTo(page, toggle)
      await page.keyboard.press('Enter')
      await expect(card).toContainText('A retry is scheduled.')
      await expect(card).not.toContainText(/Unknown error|manual/)
      await page.screenshot({ path: testInfo.outputPath(`${size}-retry-scheduled.png`) })
    })

    test('provider retry reason stays separate from an unrelated transcript error', async ({ page }, testInfo) => {
      await setupRetry(page, `retry-provider-${testInfo.workerIndex}`, 'you have reached your weekly usage limit')
      await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
        { role: 'user', content: 'Check the connection', timestamp: '2026-10-11T18:01:00Z' },
        { role: 'assistant', content: 'Error: connection refused', timestamp: '2026-10-11T18:01:01Z', is_error: true },
      ] }))
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      const cards = page.locator('.injected-card--error')
      await expect(cards).toHaveCount(2)
      await expect(cards.nth(0)).toContainText('connection refused')
      await expect(cards.nth(0)).not.toContainText('Retrying this turn')
      await expect(cards.nth(1)).toContainText('Usage limit reached')
      await expect(cards.nth(1)).toContainText('weekly usage limit')
      await expect(cards.nth(1).getByRole('button', { name: 'Try now' })).toBeVisible()
      await page.screenshot({ path: testInfo.outputPath(`${size}-separate-provider-retry.png`) })
    })

    for (const [name, action] of [['Continue with...', 'continue'], ['Try now', 'try_now'], ['Stop trying', 'stop']] as const) {
      test(`keyboard activates ${name}`, async ({ page }, testInfo) => {
        await setupRetry(page, `retry-action-${testInfo.workerIndex}`)
        await page.route('**/api/chats/alpha-chat-1/retry', route => route.fulfill({ json: { chat_id: 'alpha-chat-1', project_id: 'project-alpha', title: 'Synthetic', retry: null } }))
        await boot(page, '/chat/alpha-chat-1', COMPOSER)
        const button = page.locator('.injected-card--retry_scheduled').getByRole('button', { name, exact: true })
        await tabTo(page, button)
        if (action === 'continue') {
          await page.keyboard.press('Enter')
          await expect(page.getByText('Choose a model to retry immediately')).toBeVisible()
        } else {
          const request = page.waitForRequest(req => req.url().endsWith('/api/chats/alpha-chat-1/retry') && req.method() === 'POST')
          await page.keyboard.press('Enter')
          expect((await request).postDataJSON()).toEqual({ action })
        }
      })
    }
  })
}
