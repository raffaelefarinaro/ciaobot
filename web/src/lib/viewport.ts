/**
 * iOS viewport / keyboard plumbing.
 *
 * Drives a `--app-h` CSS variable off VisualViewport so the layout responds
 * instantly when the iOS keyboard opens/closes. `100dvh` alone does not update
 * on iOS Safari until the user interacts with the page. Also toggles a
 * `.keyboard-open` class so the home-indicator safe-area can collapse while
 * the keyboard is covering it.
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

export function installViewportPlumbing(): void {
  let maxViewportHeight = window.innerHeight

  function measure(): void {
    const vv = window.visualViewport
    const h = vv?.height ?? window.innerHeight
    maxViewportHeight = Math.max(maxViewportHeight, h)
    document.documentElement.style.setProperty('--app-h', `${h}px`)
    // With `interactive-widget=resizes-content` the layout viewport shrinks
    // along with the visual viewport, so the old `innerHeight - h > 100`
    // heuristic always returns false. Detect keyboard open by comparing
    // against the tallest viewport height we've seen for this orientation.
    const keyboardOpen = h < maxViewportHeight * KEYBOARD_OPEN_RATIO
    document.documentElement.classList.toggle('keyboard-open', keyboardOpen)
  }

  function remeasureWhileSettling(): void {
    measure()
    for (const delay of RESETTLE_DELAYS_MS) setTimeout(measure, delay)
  }

  measure()
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
