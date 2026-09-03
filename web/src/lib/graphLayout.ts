/**
 * The memory map's layout: force simulation, framing, hit testing, label
 * pressure. Pure arithmetic over plain objects.
 *
 * Lives outside the component for the same reason `cameraTween` does — the
 * component is an 1800-line canvas whose every behaviour previously needed a
 * mounted `<canvas>`, a ResizeObserver and a live store to reach. None of the
 * maths below needs any of that: it takes nodes and numbers and returns nodes
 * and numbers, so the layout can be asserted directly.
 *
 * Everything that touches a rendering context, a RAF loop, reactive camera
 * state, or the store stayed in the component. The split is "does it need the
 * browser", not "is it about the graph".
 *
 * The physics constants and their justifications moved verbatim; they were paid
 * for against a real 318-note vault and the comments record what each one fixed.
 */

import type { CameraState } from './cameraTween'

/** The part of a graph node the layout actually reads and writes. */
export interface SimNode {
  id: string
  x: number
  y: number
  vx: number
  vy: number
  degree: number
}

export interface SimEdge {
  source: string
  target: string
}

// ---------- settling ---------------------------------------------------------
//
// A hub note with 60+ links asks 60+ neighbours to all sit ~SPRING_LEN from it
// while also mutually repelling each other, which has no configuration where
// every pairwise force is simultaneously satisfied — a few nodes keep
// oscillating indefinitely under naive damping (confirmed: a 5-note test vault
// converges fine, a 318-note one with such a hub does not). A cooling schedule
// — the same technique classic force-directed layouts (Fruchterman-Reingold)
// use — bounds convergence time regardless: force output is scaled by a
// "temperature" that ramps from 1 to 0 over a fixed step budget, so velocity is
// driven to exactly zero by the time the budget elapses no matter how the
// layout was oscillating. Velocity clamping (MAX_SPEED) additionally stops any
// single step from overshooting wildly, which is what let close-packed nodes
// near a hub swing further apart each step instead of settling closer.
export const SETTLE_VELOCITY_EPS = 0.05
export const SETTLE_FRAMES_REQUIRED = 30
export const MAX_SPEED = 40

// The *visible* cooling window is a wall-clock budget, not a frame count: a
// frame-count budget converged in ~15s on a real 300-note vault instead of the
// intended "a few seconds", because each step's O(n^2) repulsion cost (and a
// software canvas) meant far fewer frames actually ran per second than assumed.
// Tying it to real elapsed time makes "how long the settle animation visibly
// runs" independent of frame rate, node count, or machine speed.
export const COOLING_DURATION_MS = 2500

/**
 * How many warmup steps a graph of this size needs before its first paint.
 *
 * A denser, hub-heavy graph needs more iterations to work out a stable
 * configuration before the user ever sees it, but the budget is capped: the
 * warmup is spread across frames and an uncapped one would keep the settling
 * visibly running long after it stopped changing anything.
 */
export function warmupStepsFor(nodeCount: number): number {
  return Math.max(80, Math.min(400, nodeCount * 3))
}

// ---------- one simulation step ---------------------------------------------

// Repulsion is weighted by degree and springs are normalized by it, because a
// uniform force model turns a real vault's degree distribution (top hubs at 65,
// 62, 50 links; median 2) into a hairball: 38% of notes ended up inside the
// middle 40% of the layout's radius, so the core was unreadable while the outer
// canvas sat empty. Two corrections, measured against the real 318-note graph:
//   - A hub's neighbours must be pushed apart from *each other*, not just from
//     the hub, so repulsion scales with both endpoints' degree.
//   - An un-normalized spring gives a 65-link note 65 inward pulls, which
//     collapses its whole neighbourhood; dividing each endpoint's pull by
//     sqrt(degree) keeps a hub from out-voting its own neighbours.
// Together these take the core fraction 38% -> 23% and raise the number of
// collision-free labels that fit at the default view by ~48%. Note that
// spreading the layout in *world* units alone does nothing for legibility —
// framing just zooms further out and the density returns; only making the
// density uniform actually buys label room.
export const MIN_GAP = 40

const REPEL = 2600
const SPRING = 0.02
const SPRING_LEN = 90
const CENTER = 0.0025
const DAMP = 0.85

/**
 * Advance the layout by one step, in place. Returns the fastest node's speed
 * this step, which is what the caller's settle detector watches.
 *
 * @param cooling 1 = full force, ramping to 0 forces velocity to zero
 *   regardless of residual imbalance.
 */
export function stepSimulation(
  nodes: SimNode[],
  edges: readonly SimEdge[],
  cooling = 1,
): number {
  const vis = nodes
  // Derived here rather than taken as an argument. Passing it in made keeping
  // it in step with `nodes` a caller obligation nothing enforced, and a stale
  // map fails in the worst way: every `byId.get` misses, so the edge loop
  // returns early for every spring and the layout silently degrades to pure
  // repulsion with no error anywhere.
  const byId = new Map(vis.map(n => [n.id, n]))
  const forces = new Map<string, { fx: number; fy: number }>()
  for (let i = 0; i < vis.length; i++) {
    const a = vis[i]
    const aw = 1 + Math.sqrt(a.degree) * 0.5
    let fx = 0
    let fy = 0
    for (let j = 0; j < vis.length; j++) {
      if (i === j) continue
      const b = vis[j]
      const dx = a.x - b.x
      const dy = a.y - b.y
      let d2 = dx * dx + dy * dy
      if (d2 < 0.01) d2 = 0.01
      const d = Math.sqrt(d2)
      let f = (REPEL / d2) * aw * (1 + Math.sqrt(b.degree) * 0.5)
      // Short-range shove: 1/d^2 is too weak to separate two nodes that are
      // already nearly coincident, which is how label-on-label pairs survived
      // an otherwise settled layout.
      if (d < MIN_GAP) f += (MIN_GAP - d) * 0.25
      fx += (dx / d) * f
      fy += (dy / d) * f
    }
    fx += -a.x * CENTER
    fy += -a.y * CENTER
    forces.set(a.id, { fx, fy })
  }
  edges.forEach(e => {
    const a = byId.get(e.source)
    const b = byId.get(e.target)
    if (!a || !b) return
    const dx = b.x - a.x
    const dy = b.y - a.y
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01
    const stretch = (d - SPRING_LEN) * SPRING
    const fx = (dx / d) * stretch
    const fy = (dy / d) * stretch
    const fa = forces.get(a.id)
    const fb = forces.get(b.id)
    const na = Math.sqrt(Math.max(1, a.degree))
    const nb = Math.sqrt(Math.max(1, b.degree))
    if (fa) { fa.fx += fx / na; fa.fy += fy / na }
    if (fb) { fb.fx -= fx / nb; fb.fy -= fy / nb }
  })
  let maxSpeed = 0
  vis.forEach(n => {
    const f = forces.get(n.id)
    if (!f) return
    n.vx = (n.vx + f.fx) * DAMP * cooling
    n.vy = (n.vy + f.fy) * DAMP * cooling
    const rawSpeed = Math.hypot(n.vx, n.vy)
    if (rawSpeed > MAX_SPEED) {
      const scale = MAX_SPEED / rawSpeed
      n.vx *= scale
      n.vy *= scale
    }
    n.x += n.vx
    n.y += n.vy
    const speed = Math.hypot(n.vx, n.vy)
    if (speed > maxSpeed) maxSpeed = speed
  })
  return maxSpeed
}

// ---------- framing ----------------------------------------------------------

export const FIT_MARGIN = 1.12
/** What the camera shows when there is nothing to frame. */
export const DEFAULT_SCALE = 0.55
const MIN_SCALE = 0.15
const MAX_SCALE = 3

/** Clamp a scale to what the camera will actually adopt. */
export function clampScale(scale: number): number {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale))
}

/**
 * The camera that frames every node in a `width` x `height` viewport.
 *
 * `scale` doubles as the graph's "fully zoomed out" reference: label thresholds
 * are expressed as how far the user has zoomed in *from* the framed view, since
 * absolute thresholds stopped being meaningful once the default zoom became
 * data-dependent.
 */
export function fitCameraFor(
  nodes: readonly SimNode[],
  width: number,
  height: number,
): CameraState {
  if (!nodes.length || !width || !height) {
    return { x: 0, y: 0, scale: DEFAULT_SCALE }
  }
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
  for (const n of nodes) {
    if (n.x < minX) minX = n.x
    if (n.x > maxX) maxX = n.x
    if (n.y < minY) minY = n.y
    if (n.y > maxY) maxY = n.y
  }
  const bw = Math.max(1, maxX - minX) * FIT_MARGIN
  const bh = Math.max(1, maxY - minY) * FIT_MARGIN
  const scale = clampScale(Math.min(width / bw, height / bh))
  const cx = (minX + maxX) / 2
  const cy = (minY + maxY) / 2
  return { x: -cx * scale, y: -cy * scale, scale }
}

// ---------- coordinates and hit testing --------------------------------------

export function worldToScreen(
  x: number,
  y: number,
  camera: CameraState,
  width: number,
  height: number,
): [number, number] {
  return [x * camera.scale + width / 2 + camera.x, y * camera.scale + height / 2 + camera.y]
}

export function screenToWorld(
  sx: number,
  sy: number,
  camera: CameraState,
  width: number,
  height: number,
): [number, number] {
  return [(sx - width / 2 - camera.x) / camera.scale, (sy - height / 2 - camera.y) / camera.scale]
}

export function nodeRadius(n: { degree: number }): number {
  return 5 + Math.min(12, Math.sqrt(n.degree + 1) * 2.7)
}

/**
 * The topmost node under a world-space point, or null.
 *
 * Walked back to front so the node drawn last — the one visibly on top — is the
 * one a click lands on.
 */
export function hitTest<T extends SimNode>(nodes: readonly T[], wx: number, wy: number): T | null {
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i]
    const r = nodeRadius(n) + 3
    const dx = n.x - wx
    const dy = n.y - wy
    if (dx * dx + dy * dy <= r * r) return n
  }
  return null
}

// ---------- label pressure ---------------------------------------------------

export const LABEL_FONT_PX = 11
export const LABEL_MAX_W = 170
export const LABEL_PAD = 3
export const LABEL_CELL = 96

// Below this zoom the canvas draws *no* labels at all: at the framed view a
// 300-note vault has no room for 300 titles, and even a handful of pills over
// the hairball read as clutter. Names stay reachable two other ways — hover
// shows one under the cursor, and zooming past this ratio brings titles back.
export const LABEL_MIN_RATIO = 1.6

/**
 * At or below this many visible notes the canvas is sparse enough that labels
 * are exempt from both the zoom gate and the degree floor.
 *
 * A small filtered view framed at fit scale has a low zoom ratio but acres of
 * free space, and hiding every title there would hide the only four notes the
 * user came to read.
 */
export const SPARSE_VIEW_NODES = 40

/**
 * Minimum degree a node needs before it may claim a label, graded by how far
 * the user has zoomed in from the framed view.
 *
 * Far out only hubs are legible at all; close in everything that fits is
 * welcome. This replaces the binary hubs-only/everything switch that made one
 * notch of zoom paint all 318 titles at once.
 */
export function labelDegreeFloor(visibleCount: number, zoomRatio: number): number {
  // A sparse view has room for everything; gating by degree there would leave a
  // local neighbourhood of leaf notes completely unlabelled.
  if (visibleCount <= SPARSE_VIEW_NODES) return 0
  if (zoomRatio >= 3.5) return 0
  if (zoomRatio >= 2.5) return 1
  return 3
}

/** Whether labels are drawn at all at this zoom, for this many nodes. */
export function labelsVisible(visibleCount: number, zoomRatio: number): boolean {
  return visibleCount <= SPARSE_VIEW_NODES || zoomRatio >= LABEL_MIN_RATIO
}

/**
 * Truncate `text` with an ellipsis so it measures at most `maxW`.
 *
 * Takes a measuring function rather than a canvas context, which is the only
 * reason this is testable: the binary search is the interesting part and it
 * does not care where the widths come from.
 */
export function ellipsize(text: string, maxW: number, measure: (s: string) => number): string {
  if (measure(text) <= maxW) return text
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (measure(text.slice(0, mid) + '…') <= maxW) lo = mid
    else hi = mid - 1
  }
  return text.slice(0, lo) + '…'
}

// ---------- colour -----------------------------------------------------------

export function hexToRgba(hex: string, a: number): string {
  const v = hex.replace('#', '')
  const r = parseInt(v.substring(0, 2), 16)
  const g = parseInt(v.substring(2, 4), 16)
  const b = parseInt(v.substring(4, 6), 16)
  return `rgba(${r},${g},${b},${a})`
}
