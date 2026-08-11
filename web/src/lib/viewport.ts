/**
 * iOS viewport / keyboard plumbing.
 *
 * Drives a `--app-h` CSS variable off VisualViewport so the layout responds
 * instantly when the iOS keyboard opens/closes. `100dvh` alone does not update
 * on iOS Safari until the user interacts with the page. Also toggles a
 * `.keyboard-open` class so the home-indicator safe-area can collapse while
 * the keyboard is covering it.
 *
 * `viewportHeight()` / `viewportWidth()` are the size any `position: fixed`
 * element should measure itself against: with `interactive-widget=resizes-content`
 * the layout viewport shrinks with the keyboard, so the visual viewport is what
 * "on screen" means. `onViewportChange()` lets code that positioned itself once
 * re-run when the keyboard opens.
 *
 * See `web/README.md` → "iOS PWA gotchas" before changing any of this.
 */

// iOS reports a stale `visualViewport.height` for a moment after the app
// resumes or rotates: the keyboard dismissal animation is still running, or JS
// was suspended while the viewport changed. One read is not enough, so
// re-measure a few times across the settling window.
const RESETTLE_DELAYS_MS = [50, 200, 500]

/** Fraction of the tallest seen viewport below which we call the keyboard open. */
const KEYBOARD_OPEN_RATIO = 0.85

/** Height of the visible viewport, i.e. excluding the on-screen keyboard. */
export function viewportHeight(): number {
  return window.visualViewport?.height ?? window.innerHeight
}

/** Width of the visible viewport. */
export function viewportWidth(): number {
  return window.visualViewport?.width ?? window.innerWidth
}

const subscribers = new Set<(height: number) => void>()

/**
 * Run `cb` whenever the viewport size changes, including keyboard open/close.
 * Returns an unsubscribe function. Installs the plumbing if it is not up yet,
 * so a component can depend on this without ordering against `main.ts`.
 */
export function onViewportChange(cb: (height: number) => void): () => void {
  installViewportPlumbing()
  subscribers.add(cb)
  return () => { subscribers.delete(cb) }
}

let maxViewportHeight = 0
let listenersAttached = false

function measure(): void {
  // The settling timers below fire up to 500ms after they are scheduled, which
  // can outlive the page — or, under vitest, the jsdom environment that owns
  // `window`. Without this guard a component that merely called
  // onViewportChange() could fail an unrelated test file with
  // "ReferenceError: window is not defined" during teardown.
  if (typeof window === 'undefined' || typeof document === 'undefined') return

  const h = viewportHeight()
  maxViewportHeight = Math.max(maxViewportHeight, h)
  document.documentElement.style.setProperty('--app-h', `${h}px`)
  // With `interactive-widget=resizes-content` the layout viewport shrinks
  // along with the visual viewport, so the old `innerHeight - h > 100`
  // heuristic always returns false. Detect keyboard open by comparing
  // against the tallest viewport height we've seen for this orientation.
  const keyboardOpen = h < maxViewportHeight * KEYBOARD_OPEN_RATIO
  document.documentElement.classList.toggle('keyboard-open', keyboardOpen)
  for (const cb of subscribers) cb(h)
}

// Tracked so repeated calls (every resume, orientation change, pageshow, and
// every lazy onViewportChange caller) cannot pile up overlapping timer sets.
let settlingTimers: ReturnType<typeof setTimeout>[] = []

function remeasureWhileSettling(): void {
  measure()
  for (const timer of settlingTimers) clearTimeout(timer)
  settlingTimers = RESETTLE_DELAYS_MS.map(delay => setTimeout(measure, delay))
}

/** Cancel pending settling timers. Exported for test teardown. */
export function stopViewportPlumbing(): void {
  for (const timer of settlingTimers) clearTimeout(timer)
  settlingTimers = []
}

/** Idempotent: safe to call from `main.ts` and lazily from `onViewportChange`. */
export function installViewportPlumbing(): void {
  if (listenersAttached) {
    // Already wired; still take a fresh reading for the new caller.
    remeasureWhileSettling()
    return
  }
  listenersAttached = true
  maxViewportHeight = window.innerHeight

  // iOS PWA: visualViewport.height can be transiently wrong on first load
  // before the standalone UI chrome settles.
  remeasureWhileSettling()

  window.addEventListener('resize', measure)
  window.addEventListener('orientationchange', () => {
    // The tallest height for the previous orientation says nothing about this
    // one, so drop it and re-derive from the new viewport.
    maxViewportHeight = 0
    remeasureWhileSettling()
  })
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', measure)
    // Intentionally not listening to `scroll`: iOS fires vv.scroll while the
    // page shifts to keep the caret visible during multi-line typing, and
    // re-reading vv.height there can latch a stale/smaller value, collapsing
    // the messages area and leaving a dead zone between the input and the
    // keyboard.
  }

  // iOS suspends JS while the PWA is backgrounded. If the keyboard was open
  // when it went away, the system dismisses it during the suspension and the
  // matching `resize` never reaches us, so `--app-h` stays latched at the
  // keyboard-open height and the layout keeps a dead zone under the input bar
  // until the user opens and closes the keyboard by hand. Re-measure on every
  // resume. Deliberately does NOT reset `maxViewportHeight`: if the keyboard
  // *is* still up at resume, resetting would record the shrunken height as the
  // maximum and the keyboard-open state would never be detected again.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') remeasureWhileSettling()
  })
  // `pageshow` covers bfcache restores (the iOS home→back pattern), where
  // `visibilitychange` often does not fire.
  window.addEventListener('pageshow', remeasureWhileSettling)

  // iOS Safari can still shift the document when the keyboard opens, leaving
  // the input bar floating with a gap below it. Lock the page scroll to 0.
  window.addEventListener('scroll', () => {
    if (window.scrollY !== 0) window.scrollTo(0, 0)
  }, { passive: true })
}
