/**
 * iOS viewport / keyboard plumbing.
 *
 * Drives a `--app-h` CSS variable off VisualViewport so the layout responds
 * instantly when the iOS keyboard opens/closes. `100dvh` alone does not update
 * on iOS Safari until the user interacts with the page. Also toggles a
 * `.keyboard-open` class so the home-indicator safe-area can collapse while
 * the keyboard is covering it, and resolves `--safe-top` so an installed PWA
 * whose webview is already below the status bar does not add that inset twice.
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

/** Slack when comparing the OS inset to the gap above the viewport, in px. */
const TOP_INSET_TOLERANCE_PX = 2

export interface TopInsetInputs {
  /** Resolved env(safe-area-inset-top) in CSS px. */
  envInset: number
  /** window.screen.height — the device's CSS-px height. */
  screenHeight: number
  /** visualViewport.height (or innerHeight). */
  viewportHeight: number
  /** visualViewport.offsetTop (0 when absent). */
  viewportOffsetTop: number
}

/**
 * The top inset the app should add: 0 when the OS already inset the webview.
 *
 * An installed PWA can be laid out below the status bar — iOS paints that band
 * itself — while `env(safe-area-inset-top)` still reports the inset. Adding it
 * again pushes the whole pane down by one status bar. When the webview is inset
 * like that, the gap between the screen and the visual viewport is already one
 * status bar tall; on a genuinely full-screen webview it is 0.
 */
export function resolveTopInset({
  envInset,
  screenHeight,
  viewportHeight,
  viewportOffsetTop,
}: TopInsetInputs): number {
  if (envInset <= 0) return 0
  const alreadyInset = screenHeight - (viewportHeight + viewportOffsetTop)
  if (alreadyInset >= envInset - TOP_INSET_TOLERANCE_PX) return 0
  return envInset
}

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

// `env()` cannot be read back from a custom property: `getComputedStyle` returns
// the raw `env(...)` token, not the resolved length. Layout is the only way to
// resolve it, so measure a throwaway box once and cache it. Rotation and resume
// re-read it because the inset changes with orientation.
let cachedEnvTopInset: number | null = null
// Avoid a style write on every resize tick while the value stays the same.
let writtenSafeTop: number | null = null

/** Resolve `env(safe-area-inset-top, 0px)` through layout, then cache it. */
function envTopInset(): number {
  if (typeof document === 'undefined') return 0
  if (cachedEnvTopInset !== null) return cachedEnvTopInset
  const probe = document.createElement('div')
  // Must not take layout space or focus, and must not intercept pointers.
  probe.style.cssText =
    'position: absolute; top: 0; left: 0; width: 0; visibility: hidden; pointer-events: none;' +
    ' height: env(safe-area-inset-top, 0px);'
  document.documentElement.appendChild(probe)
  const measured = probe.getBoundingClientRect().height
  probe.remove()
  cachedEnvTopInset = Number.isFinite(measured) ? Math.max(0, Math.round(measured)) : 0
  return cachedEnvTopInset
}

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
  // While the keyboard is up the viewport is short for a reason that has
  // nothing to do with the top safe area — the gap is the keyboard, below.
  // Reading it as "the OS already inset the webview" would drop the inset and
  // jerk every header up a status bar each time the field takes focus.
  // `window.screen.height` is already in CSS px on iOS; do not scale it by
  // devicePixelRatio.
  const safeTop = keyboardOpen
    ? envTopInset()
    : resolveTopInset({
      envInset: envTopInset(),
      screenHeight: window.screen?.height ?? h,
      viewportHeight: h,
      viewportOffsetTop: window.visualViewport?.offsetTop ?? 0,
    })
  if (safeTop !== writtenSafeTop) {
    document.documentElement.style.setProperty('--safe-top', `${safeTop}px`)
    writtenSafeTop = safeTop
  }
  const wasOpen = document.documentElement.classList.contains('keyboard-open')
  document.documentElement.classList.toggle('keyboard-open', keyboardOpen)
  for (const cb of subscribers) cb(h)
  if (wasOpen && !keyboardOpen) releaseDismissedKeyboard()
}

/**
 * The keyboard closed. If a text field still holds focus, the user swiped the
 * keyboard away rather than leaving the field, and iOS keeps the page shifted
 * for the focused field: the whole pane sits a status bar's height lower, with
 * a gap above the header, until focus moves elsewhere. Blur the field, as a
 * native app does when its keyboard is dismissed, and re-measure once the
 * viewport has settled.
 */
function releaseDismissedKeyboard(): void {
  const el = document.activeElement
  if (el instanceof HTMLElement && el.matches('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) {
    el.blur()
  }
  if (window.scrollY !== 0) window.scrollTo(0, 0)
  scheduleSettling()
}

// Tracked so repeated calls (every resume, orientation change, pageshow, and
// every lazy onViewportChange caller) cannot pile up overlapping timer sets.
let settlingTimers: ReturnType<typeof setTimeout>[] = []

function scheduleSettling(): void {
  for (const timer of settlingTimers) clearTimeout(timer)
  settlingTimers = RESETTLE_DELAYS_MS.map(delay => setTimeout(measure, delay))
}

function remeasureWhileSettling(): void {
  measure()
  scheduleSettling()
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
    // one, so drop it and re-derive from the new viewport. The safe-area inset
    // also changes with orientation, so re-read it through layout.
    maxViewportHeight = 0
    cachedEnvTopInset = null
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
