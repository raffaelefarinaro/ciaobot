import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * The composer's agents popover is laid out by the browser: jsdom cannot say
 * whether a long agent name truncates inside the 280px surface or widens its
 * row past it (which pushed the `opencode` type tag out of the popover).
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `composer-activity-${testInfo.workerIndex}`)
})

test('a long agent name truncates inside the agents popover', async ({ page }) => {
  await page.route('**/api/subagents/running', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      chats: {
        [CHAT_ID]: [{
          agent_id: 'ses_long',
          description: 'Finish analytics evidence labels and reconcile the weekly dashboard records',
          subagent_type: 'opencode',
          is_async: true,
          status: 'running',
          turn_index: 0,
        }],
      },
    }),
  }))
  await boot(page, CHAT_PATH, COMPOSER)
  await page.evaluate(async (chatId) => {
    await fetch('/__fixture__/chat-frame', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, frames: [{ type: 'tool_use', tool_use_id: 't1', tool_name: 'shell', tool_input: 'sleep 300' }] }),
    })
  }, CHAT_ID)

  const agents = page.locator('.composer-activity-seg--agents')
  await expect(agents).toBeVisible({ timeout: 8_000 })
  await agents.click()

  const pop = await page.locator('.composer-activity-popover').boundingBox()
  const item = page.locator('.composer-activity-pop-item').first()
  const row = await item.boundingBox()
  const tag = await item.locator('small').boundingBox()
  expect(row!.x + row!.width).toBeLessThanOrEqual(pop!.x + pop!.width)
  expect(tag!.x + tag!.width).toBeLessThanOrEqual(pop!.x + pop!.width)
  await page.screenshot({ path: 'test-results/composer-activity-popover.png' })
})
