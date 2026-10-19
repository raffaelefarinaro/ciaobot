import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Subagents are one trace row each (#1545). jsdom cannot say whether a long
 * name truncates and the Open link stays on the row's right edge inside the
 * live trace, so this is a browser check.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `subagent-rows-${testInfo.workerIndex}`)
})

test('live subagents render as single rows with the link inside the trace', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 900 })
  await page.route(/\/api\/chats\/[^/]+\/subagents$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([
        {
          agent_id: 'ses_running',
          description: 'Finish analytics evidence labels and reconcile every weekly dashboard record before review',
          subagent_type: 'opencode',
          status: 'running',
          messages: [],
        },
        {
          agent_id: 'ses_done',
          description: 'Audit progress tests',
          subagent_type: 'opencode',
          status: 'completed',
          messages: [
            { role: 'user', content: 'go', timestamp: '2026-10-19T10:00:00Z' },
            { role: 'assistant', content: 'done', timestamp: '2026-10-19T10:02:10Z' },
          ],
        },
    ]),
  }))
  await boot(page, CHAT_PATH, COMPOSER)
  await page.evaluate(async (chatId) => {
    await fetch('/__fixture__/chat-frame', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, frames: [{ type: 'tool_use', tool_use_id: 't1', tool_name: 'subagent', tool_input: 'Audit progress tests' }] }),
    })
  }, CHAT_ID)

  const rows = page.locator('.trace-block.live .subagent-row')
  await expect(rows).toHaveCount(2, { timeout: 8_000 })
  await expect(page.locator('.subagent-panel')).toHaveCount(0)
  await expect(rows.nth(1)).toContainText('finished')

  const trace = await page.locator('.trace-block.live .trace-body').boundingBox()
  for (let i = 0; i < 2; i++) {
    const row = await rows.nth(i).boundingBox()
    const link = await rows.nth(i).locator('.subagent-row-open').boundingBox()
    // One line, and the link inside the trace rather than pushed past it.
    expect(row!.height).toBeLessThan(40)
    expect(link!.x + link!.width).toBeLessThanOrEqual(trace!.x + trace!.width + 1)
  }
  await page.screenshot({ path: 'test-results/subagent-rows.png' })
})
