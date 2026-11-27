// Geometry of the Ciaobot mark's open-and-close motion. Pure: no Vue, no DOM.
// ringShape(p) for p in 0..1 describes the mark at that point: 0 is the resting
// C with its two dots, 1 is the closed ring. The same motion drives the website
// header (site/assets/site.js) and the Mac menu-bar mark while a chat works
// (mac/Sources/CiaobotMac/TrayMark.swift); keep them in step.
//
// The C spins, its two dots glide onto its open ends, then spread like clay
// along the opening until they meet and the ring is closed.

const CX = 630
const CY = 616
const R = 295
const A0 = -72.48 // angle of the C's upper end
const A1 = 62.0 // angle of the C's lower end
const MEET = (A0 + A1) / 2
const DOT_A = { a: -48.4, d: 361, r: 91 }
const DOT_B = { a: 38.3, d: 297, r: 56 }
const LAND = 0.35 // share of the motion spent landing the dots on the C's ends
const STROKE_R = 55 // a landed dot has the stroke's half width

export interface RingDot { cx: number; cy: number; r: number }
export interface RingShape {
  /** Rotation of the C, in degrees around the ring's centre. */
  rotate: number
  /** Growing arcs that start where each dot sits; empty until the dots have landed. */
  arcA: string
  arcB: string
  dotA: RingDot
  dotB: RingDot
}

const rad = (deg: number): number => (deg * Math.PI) / 180
const lerp = (a: number, b: number, t: number): number => a + (b - a) * t
const clamp = (t: number): number => Math.max(0, Math.min(1, t))
const smooth = (t: number): number => {
  const c = clamp(t)
  return c * c * (3 - 2 * c)
}
const inOut = (t: number): number => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)

function arc(a: number, b: number): string {
  const sweep = b > a ? 1 : 0
  const x0 = CX + R * Math.cos(rad(a))
  const y0 = CY + R * Math.sin(rad(a))
  const x1 = CX + R * Math.cos(rad(b))
  const y1 = CY + R * Math.sin(rad(b))
  return `M${x0} ${y0}A${R} ${R} 0 0 ${sweep} ${x1} ${y1}`
}

function dot(spec: { a: number; d: number; r: number }, end: number, land: number, tip: number, growing: boolean): RingDot {
  const angle = growing ? tip : lerp(spec.a, end, land)
  const dist = growing ? R : lerp(spec.d, R, land)
  return {
    cx: CX + dist * Math.cos(rad(angle)),
    cy: CY + dist * Math.sin(rad(angle)),
    r: lerp(spec.r, STROKE_R, land),
  }
}

export function ringShape(progress: number): RingShape {
  const p = clamp(progress)
  const land = smooth(p / LAND)
  const grow = inOut(clamp((p - LAND) / (1 - LAND)))
  const tipA = A0 + (MEET - A0) * grow
  const tipB = A1 + (MEET - A1) * grow
  const growing = grow > 0
  return {
    rotate: 360 * inOut(p),
    arcA: growing ? arc(A0, tipA) : '',
    arcB: growing ? arc(tipB, A1) : '',
    dotA: dot(DOT_A, A0, land, tipA, growing),
    dotB: dot(DOT_B, A1, land, tipB, growing),
  }
}

export const RING_CENTRE = { x: CX, y: CY }
