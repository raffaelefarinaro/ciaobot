/**
 * The memory map canvas's pointer gesture model.
 *
 * The canvas used to bind `mousedown`/`mousemove`/`mouseup` on `window` and
 * had no notion of a pointer at all: a touch-drag scrolled the page instead of
 * panning, `mouseup` from a second device could complete a gesture the first
 * had started, and a cancelled browser gesture (a system swipe, a scroll
 * takeover) left the graph mid-drag. This is the state a Pointer Events
 * handler needs and nothing else — no DOM, no camera, no store — so the rules
 * that matter (tap slop, cancellation, second pointers) are asserted directly
 * instead of through a mounted `<canvas>`.
 *
 * The component owns the hit test, the camera and the RAF loop; this owns only
 * "which pointer is currently allowed to drive the graph and what did it
 * intend".
 */

/** A pointer may wander this far between down and up and still be a tap. */
export const TAP_SLOP_PX = 6

export type GesturePhase = 'idle' | 'press' | 'pan' | 'node'

/** The sliver of a Pointer Event this model reads. Structural, not `PointerEvent`,
 * so a test can drive it with a plain object. */
export interface GesturePointer {
  pointerId: number
  clientX: number
  clientY: number
  /** A modifier-click that means "pick a path endpoint", not "select". */
  additive: boolean
  /** Pointer Events' isPrimary. Extra fingers are never allowed to drive the
   * graph, so a non-primary pointerdown is refused outright. */
  isPrimary?: boolean
}

export interface GesturePan {
  /** Incremental screen-space delta since the previous move, in CSS px. */
  dx: number
  dy: number
}

export type GestureMove =
  | { pan: GesturePan }
  | { dragging: string }

export type GestureTap =
  | { tap: 'node'; nodeId: string; additive: boolean }
  | { tap: 'empty' }
  | { tap: 'none' }

export class GraphGesture {
  private pointerId: number | null = null
  private startX = 0
  private startY = 0
  private lastX = 0
  private lastY = 0
  private phase: GesturePhase = 'idle'
  private nodeId: string | null = null
  private additive = false

  get active(): boolean {
    return this.pointerId !== null
  }

  get activePointerId(): number | null {
    return this.pointerId
  }

  get currentPhase(): GesturePhase {
    return this.phase
  }

  /** The node being dragged, or null. A pan is not a node drag. */
  get draggingNodeId(): string | null {
    return this.phase === 'node' ? this.nodeId : null
  }

  /**
   * Begin a gesture from a pointerdown over `nodeId` (null = empty canvas).
   *
   * Returns false when another pointer already owns the gesture or the pointer
   * is not the primary one. An ignored pointer must never reach `move`/`end`
   * as the active pointer, so a second finger cannot select a note or move a
   * node the first finger started on.
   */
  begin(p: GesturePointer, nodeId: string | null): boolean {
    if (this.pointerId !== null) return false
    if (p.isPrimary === false) return false
    this.pointerId = p.pointerId
    this.startX = p.clientX
    this.startY = p.clientY
    this.lastX = p.clientX
    this.lastY = p.clientY
    this.nodeId = nodeId
    this.additive = p.additive
    this.phase = 'press'
    return true
  }

  /**
   * Feed a pointermove.
   *
   * Returns null while the movement is within the tap slop (nothing has
   * happened yet) or when the pointer is not the active one. Past the slop the
   * gesture commits to either a pan or a node drag, which is what keeps a few
   * pixels of jitter from turning a click into a drag — and a drag from
   * activating the node it started on.
   */
  move(p: GesturePointer): GestureMove | null {
    if (p.pointerId !== this.pointerId) return null
    const dx = p.clientX - this.lastX
    const dy = p.clientY - this.lastY
    if (this.phase === 'press') {
      if (Math.hypot(p.clientX - this.startX, p.clientY - this.startY) < TAP_SLOP_PX) {
        return null
      }
      this.phase = this.nodeId ? 'node' : 'pan'
    }
    this.lastX = p.clientX
    this.lastY = p.clientY
    if (this.phase === 'node') return { dragging: this.nodeId as string }
    return { pan: { dx, dy } }
  }

  /**
   * End the active pointer. Only a gesture that never left `press` is a tap;
   * a pan or node drag releases to nothing, so the node under the finger is
   * never activated by the drag that moved it.
   */
  end(p: { pointerId: number }): GestureTap {
    if (p.pointerId !== this.pointerId) return { tap: 'none' }
    const nodeId = this.nodeId
    const additive = this.additive
    const moved = this.phase !== 'press'
    this.reset()
    if (moved) return { tap: 'none' }
    if (nodeId) return { tap: 'node', nodeId, additive }
    return { tap: 'empty' }
  }

  /**
   * Abandon the gesture without activating anything. Pointer Events reports a
   * cancelled interaction through `pointercancel` (the browser took the touch
   * for a scroll/zoom/system gesture) and `lostpointercapture`; both must end
   * the gesture cleanly rather than leave a node stuck to the finger.
   */
  cancel(p?: { pointerId: number }): boolean {
    if (this.pointerId === null) return false
    if (p && p.pointerId !== this.pointerId) return false
    this.reset()
    return true
  }

  private reset() {
    this.pointerId = null
    this.phase = 'idle'
    this.nodeId = null
    this.additive = false
  }
}
