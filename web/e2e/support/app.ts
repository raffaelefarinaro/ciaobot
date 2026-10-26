import { expect, type Browser, type BrowserContextOptions, type Page } from '@playwright/test'

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

/** The current workspace scope control at the top of the sidebar. */
export function activeWorkspaceButton(page: Page) {
  return page.locator('.workspace-scope-trigger').first()
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

/**
 * Resolve once the page has stopped changing underneath a measurement: web
 * fonts loaded, every `<link rel=stylesheet>` applied, finite animations and
 * transitions finished, and the element count and document height unchanged
 * for `frames` consecutive animation frames.
 *
 * A route's CSS arrives with its lazy chunk, and Vite's preload helper only
 * waits for a stylesheet the first time it injects it: a second import of the
 * same chunk resolves at once, so a component can render and be measured
 * before its stylesheet (including the `(pointer: coarse)` sizes) has applied.
 * Elements being visible says nothing about that. Waiting is by frame count,
 * not wall clock, so a loaded runner waits longer instead of giving up.
 */
export async function quiesce(page: Page, frames = 5): Promise<void> {
  await page.evaluate(() => {
    const w = window as unknown as { __quiet?: { sig: string; n: number } }
    w.__quiet = { sig: '', n: 0 }
  })
  await page.waitForFunction((need) => {
    const w = window as unknown as { __quiet: { sig: string; n: number } }
    const styled = Array.from(document.querySelectorAll('link[rel~="stylesheet"]')).every(
      (l) => (l as HTMLLinkElement).sheet !== null,
    )
    const animating = document.getAnimations().some((a) => {
      const t = a.effect?.getComputedTiming()
      return t !== undefined && Number.isFinite(t.endTime) && a.playState === 'running'
    })
    if (!styled || animating || document.fonts.status !== 'loaded') {
      w.__quiet.n = 0
      return false
    }
    const sig = `${document.getElementsByTagName('*').length}:${document.documentElement.scrollHeight}`
    w.__quiet.n = w.__quiet.sig === sig ? w.__quiet.n + 1 : 0
    w.__quiet.sig = sig
    return w.__quiet.n >= need
  }, frames, { polling: 'raf', timeout: 0 })
}

/** The notifications link in the expanded sidebar nav. */
export const BELL = '.nav-links a[href="/notifications"]'
/** The same link in the collapsed rail. */
export const RAIL_BELL = 'a.sidebar-rail-bell'
/** One inbox row. */
export const ROWS = '.notif-row'

/** What `POST /__fixture__/inbox` accepts; see the header of `fixture/server.mjs`. */
export interface InboxSeed {
  reset?: boolean
  chats?: Record<string, Record<string, unknown>>
  removeChats?: string[]
  tasks?: Record<string, unknown[]>
  schedules?: Record<string, Record<string, unknown>>
  removeSchedules?: string[]
  actions?: unknown[]
  events?: unknown[]
}

/**
 * Move what the notification inbox reads, and push any `events` frames to every
 * socket of this session. `page.request` shares the context's cookie jar, so
 * `isolate()` must have run first.
 */
export async function seedInbox(page: Page, seed: InboxSeed): Promise<void> {
  const res = await page.request.post('/__fixture__/inbox', { data: seed })
  expect(res.ok()).toBe(true)
}

/** Every non-GET `/api/` request this session has made, in order. */
export async function fixtureWrites(page: Page): Promise<Array<{ method: string; path: string }>> {
  const res = await page.request.get('/__fixture__/writes')
  expect(res.ok()).toBe(true)
  return (await res.json()).writes
}

/** The count a bell announces, from its accessible name: 0 for "none". */
export async function bellCount(page: Page, selector = BELL): Promise<number> {
  const label = (await page.locator(selector).first().getAttribute('aria-label')) ?? ''
  const match = /Notifications, (\d+) items?/.exec(label)
  if (match) return Number(match[1])
  if (/none/.test(label)) return 0
  throw new Error(`unrecognised bell label: ${label}`)
}

/**
 * A second device of the same install: another browser context carrying the
 * same fixture session id, so server-side events reach both.
 */
export async function secondDevice(browser: Browser, session: string, opts?: BrowserContextOptions): Promise<Page> {
  const context = await browser.newContext(opts)
  const page = await context.newPage()
  await isolate(page, session)
  return page
}
