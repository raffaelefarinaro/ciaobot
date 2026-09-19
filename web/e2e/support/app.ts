import { expect, type Page } from '@playwright/test'

/**
 * Give this browser context its own slice of the fixture's mutable state, so a
 * spec that rewrites the events snapshot cannot be seen by a spec running
 * beside it. Must be called before the first navigation — the cookie also has
 * to ride the WebSocket handshake.
 */
export async function isolate(page: Page, id: string): Promise<void> {
  await page.context().addCookies([
    { name: 'e2e_session', value: id, domain: '127.0.0.1', path: '/' },
  ])
}

/**
 * A chat title from the fixture. Its presence proves the startup overlay is
 * gone and `/api/chats` has landed, and unlike the workspace switcher it
 * exists at every width — the sidebar is not rendered at all on a phone.
 */
export const BOOTED = 'text=alpha first conversation'

/** The chat composer; the readiness signal for a `/chat/...` route. */
export const COMPOSER = 'textarea.chat-input'

/**
 * Load a route and wait until the app has finished booting.
 *
 * `ready` is a real element rather than a timer: no spec in this suite sleeps,
 * because a fixed wait is how a browser suite becomes both slow and flaky on a
 * loaded CI runner.
 */
export async function boot(page: Page, path = '/', ready: string = BOOTED): Promise<void> {
  await page.goto(path)
  await expect(page.locator(ready).first()).toBeVisible()
}

/** The workspace button the sidebar is currently showing as active. */
export function activeWorkspaceButton(page: Page) {
  return page.locator('button[aria-keyshortcuts].active').first()
}

/**
 * How far the document scrolls sideways, plus — only when it does — the
 * elements that are pushing it, so a failure names the culprit instead of
 * printing two numbers. Elements inside a region that clips or scrolls its own
 * overflow are excluded: a wide table in its own `overflow-x: auto` box is the
 * documented pattern, not a layout bug.
 */
export async function horizontalOverflow(page: Page) {
  return page.evaluate(() => {
    const root = document.documentElement
    const limit = root.clientWidth
    const overflow = Math.max(root.scrollWidth, document.body.scrollWidth) - limit
    const culprits: Array<{ tag: string; cls: string; right: number }> = []
    if (overflow > 0) {
      for (const el of Array.from(document.querySelectorAll('body *'))) {
        const rect = el.getBoundingClientRect()
        if (rect.width === 0 || rect.height === 0 || rect.right <= limit + 1) continue
        let clipped = false
        for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
          const x = getComputedStyle(p).overflowX
          if (x === 'auto' || x === 'scroll' || x === 'hidden') { clipped = true; break }
        }
        if (!clipped) culprits.push({ tag: el.tagName, cls: String(el.className).slice(0, 80), right: Math.round(rect.right) })
      }
    }
    return { limit, overflow, culprits: culprits.slice(0, 8) }
  })
}
