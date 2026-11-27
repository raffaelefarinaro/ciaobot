import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * A chat's background shells keep running after its turn ends. The engine
 * pushes the count on `/ws/events` (`chat_shells`, and the snapshot's
 * `running_shells` map), and every chat row marks it with the run glyph: the
 * sidebar row and the Home row, for a chat that is not open. A reconnect
 * applies the snapshot, so a count the engine no longer holds (a restart)
 * drops the mark.
 */
test.describe('running shell mark on chat rows', () => {
  test('sidebar and Home rows show the count, and a reconnect snapshot clears it', async ({ page, request }) => {
    const session = `shells-${test.info().workerIndex}`
    await isolate(page, session)
    const headers = { cookie: `e2e_session=${session}` }
    await request.post('/__fixture__/shells', { headers, data: { chat_id: 'alpha-chat-1', count: 0 } })
    await boot(page)

    const sidebarRow = page.locator('.chat-item', { hasText: 'alpha first conversation' })
    await expect(sidebarRow.locator('.chat-signal--shells')).toHaveCount(0)

    await request.post('/__fixture__/shells', { headers, data: { chat_id: 'alpha-chat-1', count: 3 } })
    const mark = sidebarRow.locator('.chat-signal--shells')
    await expect(mark).toHaveAttribute('aria-label', '3 shells running')
    await expect(mark.locator('.activity-glyph--run')).toBeVisible()

    // Home files the chat under Working, with the count in words on its sub-line.
    const working = page.locator('.home-tier--working')
    await expect(working.locator('.home-chat-title')).toHaveText(['alpha first conversation'])
    await expect(working.locator('.home-chat-status')).toHaveText('3 shells running')
    await expect(working.locator('.chat-signal--shells')).toHaveAttribute('aria-label', '3 shells running')

    await request.post('/__fixture__/shells', { headers, data: { chat_id: 'alpha-chat-1', count: 1 } })
    await expect(mark).toHaveAttribute('aria-label', '1 shell running')

    // The engine restarts while the page is away: its snapshot names no shell.
    const before = await (await request.get('/__fixture__/ws-count', { headers })).json()
    await request.post('/__fixture__/shells', { headers, data: { chat_id: 'alpha-chat-1', count: 0 } })
    await request.post('/__fixture__/drop-ws', { headers })
    await expect
      .poll(async () => (await (await request.get('/__fixture__/ws-count', { headers })).json()).connections)
      .toBeGreaterThan(before.connections)
    await expect(page.locator('.chat-signal--shells')).toHaveCount(0)
    await expect(page.locator('.home-tier--working')).toHaveCount(0)
  })
})
