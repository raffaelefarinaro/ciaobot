import { expect, test, type Locator, type Page } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

type RetryMode = 'literal' | 'continue_recovery'
const reason = 'you have reached your weekly usage limit'
const scheduleId = 'synthetic-schedule'
const failureId = 'synthetic-failure'
const recoveryId = 'synthetic-recovery'

async function setupRetry(page: Page, id: string, mode: RetryMode, retryReason = 'manual', verified = true) {
  await isolate(page, id)
  await page.route(/\/api\/chats(\/alpha-chat-1)?(\?.*)?$/, async route => {
    const response = await route.fetch()
    const body = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
    const withRetry = (chat: Record<string, unknown>) => chat.chat_id === 'alpha-chat-1'
      ? { ...chat, ...(mode === 'continue_recovery' ? {
          failure_id: failureId, recovery_eligible: true, user_turn_count: 1, last_error: reason,
        } : {}), retry: {
          status: 'pending', mode, schedule_id: mode === 'literal' && verified ? scheduleId : '',
          next_at: '2099-01-01T00:00:00Z', last_error: retryReason, attempts: 0, interval_seconds: 3600,
        } }
      : chat
    await route.fulfill({ response, json: Array.isArray(body) ? body.map(withRetry) : withRetry(body) })
  })
  if (mode === 'continue_recovery' && verified) {
    await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: {
      items: [
        { role: 'user', content: 'Synthetic task', images: ['synthetic.png'], turn_index: 0, i: 0 },
        { role: 'assistant', content: reason, is_error: true, i: 1 },
      ], total: 2, offset: 0, limit: 50, hasMore: false, nextOffset: null,
    } }))
  }
}

async function tabTo(page: Page, target: Locator) {
  await expect(target).toBeVisible()
  await expect(target).toBeEnabled()
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

    test('standalone literal retry is neutral and its controls remain accessible', async ({ page }, testInfo) => {
      await setupRetry(page, `retry-scheduled-${testInfo.workerIndex}`, 'literal')
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      const card = page.locator('.injected-card--retry_scheduled')
      await expect(card).toBeVisible()
      await expect(card).not.toContainText(/Unknown error|manual/)
      await expect(card).toContainText('Retrying this turn every hour')
      await expect(card.getByRole('button', { name: 'Continue with...' })).toHaveCount(0)
      for (const name of ['Run scheduled prompt', 'Stop trying']) {
        const button = card.getByRole('button', { name, exact: true })
        await expect(button).toBeEnabled()
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
      await setupRetry(page, `retry-provider-${testInfo.workerIndex}`, 'literal', reason)
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
      await expect(cards.nth(1).getByRole('button', { name: 'Run scheduled prompt' })).toBeEnabled()
      await page.screenshot({ path: testInfo.outputPath(`${size}-separate-provider-retry.png`) })
    })

    test('literal schedule without identity cannot be run or stopped', async ({ page }, testInfo) => {
      await setupRetry(page, `literal-unverified-${testInfo.workerIndex}`, 'literal', 'manual', false)
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      const card = page.locator('.injected-card--retry_scheduled')
      await expect(card.getByRole('button', { name: 'Run scheduled prompt' })).toBeDisabled()
      await expect(card.getByRole('button', { name: 'Stop trying' })).toBeDisabled()
      await expect(card).toContainText('It may still run at the scheduled time.')
    })

    test('unverified continuation is disabled but can still be stopped', async ({ page }, testInfo) => {
      await setupRetry(page, `continuation-unverified-${testInfo.workerIndex}`, 'continue_recovery', 'manual', false)
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      const card = page.locator('.injected-card--retry_scheduled')
      await expect(card.getByRole('button', { name: 'Continue with...' })).toBeDisabled()
      await expect(card.getByRole('button', { name: 'Try now' })).toBeDisabled()
      await expect(card.getByRole('button', { name: 'Stop trying' })).toBeEnabled()
      await expect(card).toContainText('The schedule may still run.')
      await page.screenshot({ path: testInfo.outputPath(`${size}-continuation-unverified.png`) })
    })

    for (const [name, action] of [['Run scheduled prompt', 'try_now'], ['Stop trying', 'stop']] as const) {
      test(`keyboard confirms guarded literal ${name}`, async ({ page }, testInfo) => {
        await setupRetry(page, `literal-${action}-${testInfo.workerIndex}`, 'literal')
        await page.route('**/api/chats/alpha-chat-1/retry', route => route.fulfill({ json: { chat_id: 'alpha-chat-1', project_id: 'project-alpha', title: 'Synthetic', retry: null } }))
        await boot(page, '/chat/alpha-chat-1', COMPOSER)
        const button = page.locator('.injected-card--retry_scheduled').getByRole('button', { name, exact: true })
        await tabTo(page, button)
        await page.keyboard.press('Enter')
        const dialog = page.getByRole('dialog')
        await expect(dialog).toBeVisible()
        const request = page.waitForRequest(req => req.url().endsWith('/api/chats/alpha-chat-1/retry') && req.method() === 'POST')
        await tabTo(page, dialog.getByRole('button', { name, exact: true }))
        await page.keyboard.press('Enter')
        expect((await request).postDataJSON()).toEqual({ action, schedule_id: scheduleId })
      })
    }

    for (const action of ['continue', 'now', 'stop'] as const) {
      test(`keyboard activates guarded continuation ${action}`, async ({ page }, testInfo) => {
        await setupRetry(page, `continuation-${action}-${testInfo.workerIndex}`, 'continue_recovery', reason)
        const posts: Record<string, unknown>[] = []
        await page.route('**/api/chats/alpha-chat-1/retry', route => {
          const body = route.request().postDataJSON() as Record<string, unknown>
          posts.push(body)
          if (body.action === 'recovery_status') return route.fulfill({ json: { status: 'pending', mode: 'continue_recovery', recovery_state: 'active', recovery_id: recoveryId } })
          if (body.action === 'issue_recovery_request') return route.fulfill({ json: { request_id: 'issued-id', expires: Date.now() / 1000 + 900 } })
          if (body.action === 'continue_recovery_now') return route.fulfill({ json: { request_id: 'issued-id', status: 'completed', result: { http_status: 200 } } })
          return route.fulfill({ json: { stopped: true } })
        })
        await boot(page, '/chat/alpha-chat-1', COMPOSER)
        const card = page.locator('.injected-card--retry_scheduled, .injected-card--error').filter({ has: page.getByRole('button', { name: 'Continue with...', exact: true }) })
        const name = action === 'continue' ? 'Continue with...' : action === 'now' ? 'Try now' : 'Stop trying'
        const button = card.getByRole('button', { name, exact: true })
        if (size === 'phone') {
          const box = (await button.boundingBox())!
          expect(box.width).toBeGreaterThanOrEqual(44)
          expect(box.height).toBeGreaterThanOrEqual(44)
        }
        await tabTo(page, button)
        await page.keyboard.press('Enter')
        if (action === 'continue') {
          await expect(page.getByText('Continue with the current model')).toBeVisible()
          expect(posts).toEqual([])
          await page.screenshot({ path: testInfo.outputPath(`${size}-continuation-picker.png`) })
        } else if (action === 'now') {
          await expect.poll(() => posts.length).toBe(2)
          expect(posts).toEqual([{ action: 'issue_recovery_request' }, { action: 'continue_recovery_now', failure_id: failureId, request_id: 'issued-id' }])
        } else {
          await expect(page.getByRole('dialog')).toBeVisible()
          await tabTo(page, page.getByRole('dialog').getByRole('button', { name: 'Stop trying' }))
          await page.keyboard.press('Enter')
          await expect.poll(() => posts.length).toBe(2)
          expect(posts).toEqual([{ action: 'recovery_status' }, { action: 'stop_recovery', recovery_id: recoveryId }])
        }
      })
    }
  })
}
