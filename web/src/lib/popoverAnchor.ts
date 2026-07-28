/**
 * Viewport clamping for `position: fixed` popovers.
 *
 * Anything a fixed popover pushes past the viewport edge is unreachable — the
 * page cannot be scrolled to it. Every popover anchored to a selection or an
 * element clamps through here so the fix lives in one place.
 */

export const ANCHOR_PAD = 8

/** Clamp a top offset so a `reserve`-tall popover stays fully on screen. */
export function clampAnchorTop(top: number, reserve: number, pad = ANCHOR_PAD): number {
  return Math.min(Math.max(pad, top), Math.max(pad, window.innerHeight - reserve))
}

/** Clamp a left offset so a `width`-wide popover stays fully on screen. */
export function clampAnchorLeft(left: number, width: number, pad = ANCHOR_PAD): number {
  return Math.min(Math.max(pad, left), Math.max(pad, window.innerWidth - width - pad))
}
