import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Delegating a task lands on the delegated chat. The task page, or the overlay a
 * task link opened over a chat, ends with the delegation instead of staying open
 * behind or reappearing over the conversation; focus is in the chat composer; Back
 * returns to where the user was. Focus, history and what stays mounted are browser
 * facts, so this runs against the real router at desktop and phone widths.
 */
const TASK_ID = '0123456789abcdef0123456789abcdef'
const DELEGATED_CHAT = 'alpha-chat-2'

/**
 * Focus after landing. Wide panes take the composer through ChatPanel's own
 * policy; on a phone it must not (the keyboard would rise), so focus sits on the
 * composer group, a landmark that is not a text field. Never on <body>.
 */
async function expectFocusLanded(page: Page, phone: boolean): Promise<void> {
  if (phone) {
    await expect(page.getByRole('group', { name: 'Message composer' })).toBeFocused()
    await expect(page.locator(COMPOSER)).not.toBeFocused()
  } else {
    await expect(page.locator(COMPOSER)).toBeFocused()
  }
  await expect(page.locator('body')).not.toBeFocused()
}

/** Answer the delegate POST as the engine does: a running attempt in an existing chat. */
async function answerDelegation(page: Page): Promise<void> {
  await page.route('**/api/tasks/*/delegate', async route => {
    const read = await route.fetch({ method: 'GET', url: `/api/tasks/${TASK_ID}?workspace=alpha`, postData: undefined })
    const { task } = await read.json()
    const attempt = {
      attempt_id: 'f'.repeat(32), task_id: TASK_ID, task_revision: task.revision, chat_id: DELEGATED_CHAT,
      state: 'running', created_at: '2026-03-02T09:00:00+00:00', updated_at: '2026-03-02T09:00:00+00:00',
      ended_at: '', detail: '', live: true,
    }
    await route.fulfill({
      json: {
        workspace: 'alpha', created: true, attempt, chat_id: DELEGATED_CHAT, project_id: 'general', project_origin: 'general',
        task: { ...task, status: 'in_progress', assignee: 'agent', chat_id: DELEGATED_CHAT, attempt_id: attempt.attempt_id, attempt_state: 'running', live_attempt_id: attempt.attempt_id },
      },
    })
  })
}

for (const viewport of [{ width: 1280, height: 900, phone: false }, { width: 390, height: 844, phone: true }]) {
  test.describe(`at ${viewport.width}px`, () => {
    test.use({ viewport, isMobile: viewport.phone, hasTouch: viewport.phone })

    test('a task link over a chat: delegating ends the overlay and opens the chat', async ({ page }, testInfo) => {
      await isolate(page, `delegate-land-overlay-${testInfo.workerIndex}-${viewport.width}`)
      await answerDelegation(page)
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      await page.evaluate(() => fetch('/__fixture__/transcript', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ shape: 'task-link' }),
      }))
      await page.reload()
      await page.waitForSelector(COMPOSER)
      await page.locator('.message-wrap.assistant a', { hasText: 'Reply to Ivo' }).click()
      await page.locator('.task-delegate-actions button', { hasText: 'Delegate to agent' }).click()
      const sheet = page.locator('.task-delegate-sheet')
      await expect(sheet).toBeVisible()
      await page.locator('#task-delegate-instructions').fill('Write it in Spanish.')
      await page.screenshot({ path: testInfo.outputPath(`overlay-sheet-${viewport.width}.png`) })
      await sheet.getByRole('button', { name: 'Delegate', exact: true }).click()

      await expect(page).toHaveURL(new RegExp(`/chat/${DELEGATED_CHAT}$`))
      await expect(page.locator('.task-delegate-sheet')).toHaveCount(0)
      await expect(page.locator('.task-detail-sheet')).toHaveCount(0)
      await expectFocusLanded(page, viewport.phone)
      await page.screenshot({ path: testInfo.outputPath(`overlay-landed-${viewport.width}.png` ) })

      // Back is the chat the link was clicked in, with nothing of the task over it.
      await page.goBack()
      await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
      await expect(page.locator('.task-delegate-sheet')).toHaveCount(0)
      await expect(page.locator('.task-detail-sheet')).toHaveCount(0)
    })

    test('the task page: delegating closes it and Back returns to the board', async ({ page }, testInfo) => {
      await isolate(page, `delegate-land-page-${testInfo.workerIndex}-${viewport.width}`)
      await answerDelegation(page)
      await boot(page, '/tasks', '.task-view')
      await page.locator('.task-card-head').first().click()
      await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`))
      await page.locator('.task-delegate-actions button', { hasText: 'Delegate to agent' }).click()
      const sheet = page.locator('.task-delegate-sheet')
      await expect(sheet).toBeVisible()
      await sheet.getByRole('button', { name: 'Delegate', exact: true }).click()

      await expect(page).toHaveURL(new RegExp(`/chat/${DELEGATED_CHAT}$`))
      await expect(page.locator('.task-delegate-sheet')).toHaveCount(0)
      await expect(page.locator('.task-detail-sheet')).toHaveCount(0)
      await expectFocusLanded(page, viewport.phone)
      await page.screenshot({ path: testInfo.outputPath(`page-landed-${viewport.width}.png`) })

      await page.goBack()
      await expect(page).toHaveURL(/\/tasks$/)
      await expect(page.locator('.task-detail-sheet')).toHaveCount(0)
      await expect(page.locator('.task-open').first()).toBeVisible()
    })
  })
}
