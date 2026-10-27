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

test.describe('native shell row', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('lists a running native shell with its age and a Stop that fits a touch target', async ({ page }) => {
    const startedAt = new Date(Date.now() - 65 * 60_000).toISOString()
    let stopped = ''
    await page.route('**/api/subagents/running', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        chats: {
          [CHAT_ID]: [{
            agent_id: 'task:by55qns1w',
            description: "bash -c 'ping -c 4200 127.0.0.1 >/dev/null; echo SOAK_DONE'",
            subagent_type: 'shell',
            is_async: true,
            status: 'running',
            turn_index: null,
            started_at: startedAt,
            stoppable: true,
          }],
        },
      }),
    }))
    await page.route('**/api/chats/*/background-tasks/*/stop', route => {
      stopped = route.request().url()
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
    })
    await boot(page, CHAT_PATH, COMPOSER)

    const seg = page.locator('.composer-activity-seg--runs')
    await expect(seg).toBeVisible({ timeout: 8_000 })
    await seg.click()
    const row = page.locator('.shell-row').first()
    await expect(row).toContainText('1h 5m')
    const stop = row.locator('.shell-row-stop')
    const pop = (await page.locator('.composer-activity-popover').boundingBox())!
    const box = (await stop.boundingBox())!
    expect(box.height).toBeGreaterThanOrEqual(44)
    expect(box.x + box.width).toBeLessThanOrEqual(pop.x + pop.width)
    await page.screenshot({ path: test.info().outputPath('native-shell-row.png') })

    await stop.click()
    await expect(stop).toHaveText('Stopping…')
    expect(stopped).toContain('/background-tasks/by55qns1w/stop')
  })

  test('lists the native shell in the Chat details rail with its age and a 44px Stop', async ({ page }) => {
    const startedAt = new Date(Date.now() - 65 * 60_000).toISOString()
    let stopped = ''
    await page.route('**/api/subagents/running', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        chats: {
          [CHAT_ID]: [{
            agent_id: 'task:by55qns1w', description: 'npm run dev', subagent_type: 'shell',
            is_async: true, status: 'running', turn_index: null, started_at: startedAt, stoppable: true,
          }],
        },
      }),
    }))
    await page.route('**/api/chats/*/background-tasks/*/stop', route => {
      stopped = route.request().url()
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
    })
    await boot(page, CHAT_PATH, COMPOSER)

    await page.locator('.composer-activity-seg--runs').click()
    await page.locator('.composer-activity-pop-foot').click()
    const rail = page.locator('.chat-rail-running')
    await expect(rail).toBeVisible()
    await expect(rail.locator('.chat-rail-subhead')).toHaveText('Processes')
    const row = rail.locator('.shell-row')
    await expect(row).toContainText('npm run dev')
    await expect(row).toContainText('1h 5m')
    const stop = row.locator('.shell-row-stop')
    const box = (await stop.boundingBox())!
    expect(box.height).toBeGreaterThanOrEqual(44)
    const railBox = (await rail.boundingBox())!
    expect(box.x + box.width).toBeLessThanOrEqual(railBox.x + railBox.width)
    // The capture is for a person to look at: let the sheet finish sliding in.
    await page.waitForTimeout(600)
    await page.screenshot({ path: test.info().outputPath('rail-shell-row.png') })

    await stop.click()
    await expect(stop).toHaveText('Stopping…')
    expect(stopped).toContain('/background-tasks/by55qns1w/stop')
  })
})
