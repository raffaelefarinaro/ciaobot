import { expect, test, type Page } from '@playwright/test'
import { boot, isolate, COMPOSER } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The failure in #619 was never the store or ChatPanel: both already handled an
 * archived chat. It was that `/chat/<archived-id>` resolved to no panel at all,
 * so the archived footer — the memory-pass link and "Continue in new chat" —
 * was unreachable. Only a full navigation can show
 * that: the boot URL restore, ChatLayout's route watcher and its
 * `v-else-if="store.activeChat"` all had to agree. A vitest test drives one of
 * them with a stubbed router and a hand-seeded store, and a regression in any
 * of the other two would leave it green.
 *
 * "Inert" is the other half, and it is also only observable here: it is a
 * statement about which sockets the page actually dialled. The store test can
 * assert that `openChatFromDeepLink` returns without calling `connectWs`, but
 * not that no other path dialled one on the way.
 */
const ARCHIVED_CHAT = 'alpha-chat-3'
const LIVE_CHAT = 'alpha-chat-1'

/**
 * Record every URL the page dials, from before the app's first script runs.
 *
 * The fixture server counts only `/ws/events` connections (it accepts
 * per-chat sockets and stays quiet), so the absence of a `/ws/chat/...` dial
 * has to be observed from the client side.
 */
async function recordWebSockets(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __wsUrls: string[]; WebSocket: typeof WebSocket }
    const seen: string[] = []
    w.__wsUrls = seen
    const Native = w.WebSocket
    w.WebSocket = class extends Native {
      constructor(url: string | URL, protocols?: string | string[]) {
        seen.push(String(url))
        super(url, protocols)
      }
    } as typeof WebSocket
  })
}

function dialled(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __wsUrls: string[] }).__wsUrls)
}

test.describe('archived chat', () => {
  test('a deep link opens it read-only, with no composer and no chat socket', async ({ page }) => {
    await isolate(page, `archived-${test.info().workerIndex}`)
    await recordWebSockets(page)

    // The bug rendered a blank pane with the URL still saying /chat/..., so the
    // archived notice is the assertion, not an incidental detail.
    await boot(page, `/chat/${ARCHIVED_CHAT}`, '.archived-notice')
    await expect(page.locator('.archived-notice')).toContainText('This chat is archived.')
    await expect(page.locator('.continue-chat-btn')).toBeVisible()
    // #618 added the memory-pass link to this same footer, so it was
    // unreachable for the same reason; it renders off the memory-pass record the
    // chat carries, not off any live data.
    await expect(page.locator('.archive-memory-pass-btn')).toHaveText('Open memory pass')
    // Inert, not merely hidden: there is no way to send anything to a session
    // the provider has already reclaimed.
    await expect(page.locator(COMPOSER)).toHaveCount(0)

    const archivedDials = await dialled(page)
    // The recorder is live — the awareness socket is always dialled — which is
    // what makes the empty per-chat list below a decision and not a dead probe.
    expect(archivedDials.some(url => url.includes('/ws/events'))).toBe(true)
    expect(archivedDials.filter(url => url.includes('/ws/chat/'))).toEqual([])

    // The contrast: the same journey onto a live chat does dial its socket.
    await boot(page, `/chat/${LIVE_CHAT}`, COMPOSER)
    await expect
      .poll(async () => (await dialled(page)).some(url => url.includes(`/ws/chat/${LIVE_CHAT}`)))
      .toBe(true)
  })

  /**
   * The project's own archive list, and the file viewer it opens (#1710).
   *
   * A browser test because the failure is a layout one: the fixture had no
   * route for `GET /api/projects/<id>/chats?archived=1`, so the list came back
   * as `{}`, ProjectView read `items` off a payload that had none, and the
   * whole project pane went blank instead of listing the archived chats.
   *
   * It is also the only place #1700 shows up for real. That fix kept the
   * pinned side tile beside a chat; a project route must not become one, or
   * the transcript opens in the tile and the file viewer — with its
   * "Continue this chat" — never opens at all. jsdom has no split view and
   * no `.chat-split-side`, so the regression is only observable here.
   *
   * What this asserts is the list and the surface that opens it. The
   * fixture's chat ids predate the engine's `chat-<8hex>` shape, and the
   * transcript folder carries the `chat-` prefix so the viewer offers
   * "Continue this chat" at all — which means the button names a chat the
   * fixture has no row for. So the continue round-trip itself is out of
   * reach here, and is not what this spec claims.
   */
  test('the project archive list opens its transcript in the viewer, not the side panel', async ({ page }) => {
    await isolate(page, `project-archive-${test.info().workerIndex}`)
    // Desktop width: the split view exists only there, so a transcript that
    // docked instead of opening would be visible.
    await page.setViewportSize({ width: 1280, height: 900 })

    await boot(page, '/project/alpha-general', '.archive-toggle')
    await page.locator('.archive-toggle').click()

    const row = page.locator('.chat-row.archived', { hasText: 'alpha archived conversation' })
    await expect(row).toBeVisible()
    await row.click()

    const viewer = page.locator('.fv-modal')
    await expect(viewer).toBeVisible()
    // The action that makes an archived transcript worth opening, and the
    // reason the file viewer is the right surface for it.
    await expect(viewer.locator('.fv-continue-btn')).toHaveText('Continue this chat')
    // The side tile is the chat's own pinned pane (#1700). It has no business
    // on a project route, where it would have taken the transcript.
    await expect(page.locator('.chat-split-side')).toHaveCount(0)
  })
})
