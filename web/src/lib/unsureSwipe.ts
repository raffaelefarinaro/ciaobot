/**
 * The phone queue's swipe: a card dragged right approves, dragged left
 * dismisses. Pure numbers, no DOM, so the thresholds are tested on their own.
 *
 * A press becomes a drag only once it has moved `SWIPE_SLOP_PX` and mostly
 * sideways; a mostly vertical move is a scroll and is left to the browser. On
 * release the card commits when it travelled `swipeThreshold(width)` (a
 * quarter of the card, at least `SWIPE_MIN_PX`), or when a shorter fling was
 * fast enough; anything less springs back.
 */

export type SwipeOutcome = 'approve' | 'dismiss' | null

/** Movement before a press is read as a drag or a scroll. */
export const SWIPE_SLOP_PX = 10
/** The shortest committing drag, whatever the card's width. */
export const SWIPE_MIN_PX = 80
/** The share of the card's width a slow drag must cover. */
export const SWIPE_RATIO = 0.28
/** A fling commits early: this fast (px per ms) and at least `SWIPE_FLING_MIN_PX` long. */
export const SWIPE_FLING_SPEED = 0.6
export const SWIPE_FLING_MIN_PX = 40

export function swipeThreshold(width: number): number {
  return Math.max(SWIPE_MIN_PX, Math.round(width * SWIPE_RATIO))
}

/** Whether a press that has moved this far is a sideways drag (true), a scroll (false), or too small to say (null). */
export function swipeAxis(dx: number, dy: number): boolean | null {
  if (Math.hypot(dx, dy) < SWIPE_SLOP_PX) return null
  return Math.abs(dx) > Math.abs(dy) * 1.2
}

/** What a released drag does: `dx` its travel, `ms` how long it took, `width` the card's. */
export function swipeOutcome(dx: number, ms: number, width: number): SwipeOutcome {
  const distance = Math.abs(dx)
  const speed = ms > 0 ? distance / ms : 0
  const commits = distance >= swipeThreshold(width) || (distance >= SWIPE_FLING_MIN_PX && speed >= SWIPE_FLING_SPEED)
  if (!commits) return null
  return dx > 0 ? 'approve' : 'dismiss'
}

/** How far toward committing the drag is, -1 (dismiss) to 1 (approve): the stamp's strength. */
export function swipeProgress(dx: number, width: number): number {
  return Math.max(-1, Math.min(1, dx / swipeThreshold(width)))
}

/** The card's transform mid-drag: it follows the finger and, unless motion is reduced, tilts with it. */
export function swipeTransform(dx: number, width: number, reducedMotion: boolean): string {
  if (!dx) return ''
  const tilt = reducedMotion ? 0 : Math.max(-12, Math.min(12, (dx / Math.max(width, 1)) * 14))
  return tilt ? `translateX(${dx}px) rotate(${tilt.toFixed(2)}deg)` : `translateX(${dx}px)`
}
