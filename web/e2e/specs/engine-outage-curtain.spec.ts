import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The outage curtain is wired from three places: the engine monitor decides,
 * the app hands it the socket's proof of life, and the socket produces that
 * proof. A vitest test can only assert against a hand-written fake; here the
 * real page, the real store and a real WebSocket have to agree, because the
 * bug this guards is precisely a disagreement between them: the monitor
 * believed a failed HTTP probe and painted "Ciaobot on <host> isn't
 * reachable" over a perfectly healthy engine, while the awareness socket was
 * still delivering frames.
 *
 * The fixture answers the API with invented data and never touches a vault,
 * a model or a credential.
 */

/** The curtain the engine monitor raises. */
const CURTAIN = '.engine-offline'

/** The awareness socket, mocked to behave like the engine's: it pushes a frame. */
async function mockAwarenessSocket(page: Page) {
  await page.routeWebSocket('**/ws/events', (ws) => {
    const timer = setInterval(() => {
      try {
        ws.send(JSON.stringify({ type: 'keepalive' }))
      } catch {
        clearInterval(timer)
      }
    }, 2000)
    ws.onClose(() => clearInterval(timer))
  })
}

test.describe('engine outage curtain', () => {
  // The monitor probes every 10s while it is healthy and retries every 2s
  // once a probe fails, so the second failure lands around t+12s. Waiting
  // past that is what makes the assertion mean something: a monitor that
  // ignored the socket would already have raised the curtain by then.
  test('a failing probe does not raise the curtain while the socket is alive', async ({ page }) => {
    test.setTimeout(60_000)
    await isolate(page, 'outage-live-socket')
    await mockAwarenessSocket(page)
    // The path is broken: every probe comes back with a gateway error, the
    // answer a proxy in front of the engine gives when it cannot reach it.
    await page.route('**/api/startup-status', (route) => route.fulfill({
      status: 504,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'gateway timeout' }),
    }))

    await boot(page)
    await expect(page.locator(CURTAIN)).toHaveCount(0)

    // Well past the point where two probes have failed.
    await page.waitForTimeout(16_000)

    // The engine never stopped talking: the socket kept delivering frames, so
    // the curtain must not be up.
    await expect(page.locator(CURTAIN)).toHaveCount(0)
    // And the app underneath it is still the app, not a blank screen.
    await expect(page.locator('text=alpha first conversation').first()).toBeVisible()
  })

  test('raises the curtain when nothing can vouch for the engine', async ({ page }) => {
    test.setTimeout(60_000)
    await isolate(page, 'outage-dead-engine')
    // No socket: the engine never answers and nothing proves it is alive.
    await page.routeWebSocket('**/ws/events', (ws) => ws.close({ code: 4001 }))
    await page.route('**/api/startup-status', (route) => route.fulfill({
      status: 502,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'bad gateway' }),
    }))

    await boot(page)
    await expect(page.locator(CURTAIN)).toHaveCount(0)

    // A dead engine is still a dead engine: the curtain must come up.
    await expect(page.locator(CURTAIN)).toBeVisible({ timeout: 30_000 })
  })
})
