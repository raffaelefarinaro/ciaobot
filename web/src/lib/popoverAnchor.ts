/**
 * Viewport clamping for `position: fixed` popovers.
 *
 * Anything a fixed popover pushes past the viewport edge is unreachable — the
 * page cannot be scrolled to it. Every popover anchored to a selection or an
 * element clamps through here so the fix lives in one place.
 *
 * The bound is the *visual* viewport, not `window.innerHeight`: when the
 * on-screen keyboard is up, the visible area is shorter, and clamping against
 * the full window height puts the popover behind the keyboard. Callers that can
 * still be on screen when the keyboard opens should pass a reactive height (see
 * `composables/useViewportHeight.ts`) so they re-clamp instead of measuring the
 * viewport once at open time.
 */

import { viewportHeight, viewportWidth } from './viewport'

const ANCHOR_PAD = 8

/** Clamp a top offset so a `reserve`-tall popover stays fully on screen. */
export function clampAnchorTop(
  top: number,
  reserve: number,
  availableH: number = viewportHeight(),
  pad = ANCHOR_PAD,
): number {
  return Math.min(Math.max(pad, top), Math.max(pad, availableH - reserve))
}

/** Clamp a left offset so a `width`-wide popover stays fully on screen. */
export function clampAnchorLeft(
  left: number,
  width: number,
  availableW: number = viewportWidth(),
  pad = ANCHOR_PAD,
): number {
  return Math.min(Math.max(pad, left), Math.max(pad, availableW - width - pad))
}
