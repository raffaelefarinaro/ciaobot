import { expect, type Browser, type BrowserContextOptions, type Locator, type Page } from '@playwright/test'

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

export interface BootOptions {
  /** Awaited before navigation, so a test can set up state for this load. */
  beforeNavigate?: () => Promise<void>
  /**
   * Awaited after navigation and before the readiness wait. The seam a test
   * uses to inspect a surface that is still loading, or to hold it there.
   */
  beforeReady?: () => Promise<void>
  /** Called in the same turn the readiness wait starts, after `beforeReady`. */
  onReadyWait?: () => void
  /** Bound on the readiness wait, so a test can prove that it blocks. */
  readyTimeout?: number
}

/**
 * Load a route and wait until the app has finished booting.
 *
 * `ready` is a real element rather than a timer: no spec in this suite sleeps,
 * because a fixed wait is how a browser suite becomes both slow and flaky on a
 * loaded CI runner.
 */
export async function boot(
  page: Page,
  path = '/',
  ready: string = BOOTED,
  options: BootOptions = {},
): Promise<void> {
  await options.beforeNavigate?.()
  await page.goto(path)
  await options.beforeReady?.()
  options.onReadyWait?.()
  await expect(page.locator(ready).first())
    .toBeVisible(options.readyTimeout === undefined ? undefined : { timeout: options.readyTimeout })
}

/** The current workspace scope control at the top of the sidebar. */
export function activeWorkspaceButton(page: Page) {
  return page.locator('.workspace-scope-trigger').first()
}

/**
 * Finish an active pointer drag with a destination move and release in the same
 * JavaScript task, before Vue can flush the preview. The caller must already
 * have pressed and activated the drag with real mouse events; this keeps the
 * browser's layout and hit testing while forcing the pending-render seam.
 */
export async function releasePointerInSameTask(
  root: Locator,
  point: { x: number; y: number },
  pointerId = 1,
): Promise<void> {
  await root.evaluate(
    (el, arg) => {
      const init: PointerEventInit = {
        pointerId: arg.pointerId,
        isPrimary: true,
        pointerType: 'mouse',
        button: 0,
        buttons: 1,
        clientX: arg.point.x,
        clientY: arg.point.y,
        bubbles: true,
        cancelable: true,
      }
      el.dispatchEvent(new PointerEvent('pointermove', init))
      el.dispatchEvent(new PointerEvent('pointerup', { ...init, buttons: 0 }))
    },
    { point, pointerId },
  )
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

/** The notifications bell in the expanded sidebar's brand row. */
export const BELL = '.sidebar-brand-row .notif-bell'
/** The same bell in the collapsed rail. */
export const RAIL_BELL = '.sidebar-history-rail .notif-bell'
/** The bell in the pane header, which only shows below 768px. */
export const HEADER_BELL = '.header-trail .notif-bell'
/** The inbox popover (or sheet) a bell opens. */
export const INBOX_DIALOG = '[role="dialog"][aria-label="Notifications"]'
/** One inbox row. */
export const ROWS = '.notif-row'

/**
 * Open the inbox from the bell the viewport shows, and wait for its dialog.
 * Pass `bell` to name one: at phone width the header bell is the visible one.
 */
export async function openInbox(page: Page, bell?: string): Promise<Locator> {
  const trigger = bell ? page.locator(bell) : page.getByRole('button', { name: /^Notifications/ })
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: 'Notifications' })
  await expect(dialog).toBeVisible()
  return dialog
}

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
