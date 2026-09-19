import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The awareness socket's recovery is a browser behaviour end to end: the
 * engine notices the TCP connection died, fires `onclose`, and the store
 * re-dials and re-applies whatever snapshot the new socket hands it. A vitest
 * test can only assert against a hand-written fake whose `close()` it called
 * itself — it cannot prove the browser ever noticed the drop, and it cannot
 * catch a regression where the reconnect fires but the snapshot is ignored.
 *
 * The fixture runs a real (dependency-free) RFC 6455 server and severs the
 * socket at the TCP level, which is what a backgrounded PWA or a restarted
 * host looks like from the page's side. No model, no credential, no vault.
 */
test.describe('events websocket', () => {
  test('reconnects after the socket drops and applies the new snapshot', async ({ page, request }) => {
    const session = `reconnect-${test.info().workerIndex}`
    await isolate(page, session)
    const headers = { cookie: `e2e_session=${session}` }

    // The host says a chat is mid-turn, so the sidebar shows a Working signal.
    await request.post('/__fixture__/streams', { headers, data: { chat_ids: ['alpha-chat-1'] } })
    await boot(page)
    await expect(page.locator('[aria-label="Working"]').first()).toBeVisible()

    const before = await (await request.get('/__fixture__/ws-count', { headers })).json()
    expect(before.open).toBeGreaterThan(0)

    // The turn finishes while the page is disconnected — the exact gap the
    // snapshot exists to heal.
    await request.post('/__fixture__/streams', { headers, data: { chat_ids: [] } })
    const drop = await (await request.post('/__fixture__/drop-ws', { headers })).json()
    expect(drop.dropped).toBeGreaterThan(0)

    // A new socket, not the old one resurrected.
    await expect
      .poll(async () => (await (await request.get('/__fixture__/ws-count', { headers })).json()).connections)
      .toBeGreaterThan(before.connections)

    // ...and the snapshot it carried actually reached the UI. Without this the
    // test would pass on a client that reconnects and then ignores the frame.
    await expect(page.locator('[aria-label="Working"]')).toHaveCount(0)
  })
})
