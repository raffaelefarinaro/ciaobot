/**
 * Camera interpolation for the memory map's canvas.
 *
 * Focusing a note used to assign `camera.x/y` directly, which teleports the
 * graph: clicking an entry in "Most connected" replaced the view with a
 * different-looking hairball and gave no clue which dot had been centred, and
 * the same jump happened on every canvas click since a click focuses too.
 * Animating the move is what makes the destination readable — the eye can
 * follow a dot across a pan, it cannot follow a cut.
 *
 * Lives outside the component because it is pure arithmetic and the component
 * is a 1300-line canvas with no unit tests of its own.
 */

export interface CameraState {
  x: number
  y: number
  scale: number
}

/** Decelerating ease: fast off the mark, gentle on arrival. */
export function easeOutCubic(t: number): number {
  const c = Math.min(1, Math.max(0, t))
  return 1 - Math.pow(1 - c, 3)
}

/**
 * Camera state a fraction `t` (0..1, already eased) of the way from `from` to
 * `to`.
 *
 * Scale interpolates geometrically, not linearly: zoom is perceived as a ratio,
 * so a linear ramp from 0.5 to 3 spends most of its frames already zoomed in
 * and reads as a lurch. The translation is interpolated in *screen* space,
 * which is what the camera stores — the world point under the viewport centre
 * therefore travels smoothly even while the scale is changing.
 */
export function tweenCamera(from: CameraState, to: CameraState, t: number): CameraState {
  const c = Math.min(1, Math.max(0, t))
  const fromScale = from.scale > 0 ? from.scale : 1
  const toScale = to.scale > 0 ? to.scale : 1
  return {
    x: from.x + (to.x - from.x) * c,
    y: from.y + (to.y - from.y) * c,
    scale: fromScale * Math.pow(toScale / fromScale, c),
  }
}

/** True when the OS asks for reduced motion — every tween then snaps instead. */
export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}
