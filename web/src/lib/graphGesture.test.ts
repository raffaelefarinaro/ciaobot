/**
 * The canvas gesture rules, asserted without a canvas.
 *
 * Every property here is one the mouse-only implementation got wrong in a way
 * only real touch hardware exposed: page scroll fighting the pan, a second
 * finger activating a note, and a browser-cancelled gesture leaving a node
 * stuck under the finger.
 */
import { describe, expect, it } from 'vitest'
import { GraphGesture, TAP_SLOP_PX, type GesturePointer } from './graphGesture'

function pointer(over: Partial<GesturePointer> = {}): GesturePointer {
  return { pointerId: 1, clientX: 0, clientY: 0, additive: false, isPrimary: true, ...over }
}

describe('GraphGesture', () => {
  it('a press-and-release in place is a node tap', () => {
    const g = new GraphGesture()
    expect(g.begin(pointer(), 'n1')).toBe(true)
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'node', nodeId: 'n1', additive: false })
  })

  it('a press on empty canvas releases as an empty tap', () => {
    const g = new GraphGesture()
    g.begin(pointer(), null)
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'empty' })
  })

  it('jitter within the slop still counts as a tap', () => {
    const g = new GraphGesture()
    g.begin(pointer(), 'n1')
    expect(g.move(pointer({ clientX: TAP_SLOP_PX - 1 }))).toBeNull()
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'node', nodeId: 'n1', additive: false })
  })

  it('moving past the slop over a node becomes a node drag, not a tap', () => {
    const g = new GraphGesture()
    g.begin(pointer(), 'n1')
    expect(g.move(pointer({ clientX: TAP_SLOP_PX + 1 }))).toEqual({ dragging: 'n1' })
    expect(g.currentPhase).toBe('node')
    // The release that ends a drag must not also activate the node.
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'none' })
  })

  it('moving past the slop over empty canvas pans by the screen delta', () => {
    const g = new GraphGesture()
    g.begin(pointer(), null)
    const move = g.move(pointer({ clientX: 10, clientY: 4 }))
    expect(move).toEqual({ pan: { dx: 10, dy: 4 } })
    expect(g.currentPhase).toBe('pan')
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'none' })
  })

  it('pan deltas are incremental, not cumulative', () => {
    const g = new GraphGesture()
    g.begin(pointer(), null)
    g.move(pointer({ clientX: 20, clientY: 0 }))
    expect(g.move(pointer({ clientX: 26, clientY: -3 }))).toEqual({ pan: { dx: 6, dy: -3 } })
  })

  it('a second pointer is refused while the first gesture is live', () => {
    const g = new GraphGesture()
    g.begin(pointer({ pointerId: 1 }), 'n1')
    expect(g.begin(pointer({ pointerId: 2 }), 'n2')).toBe(false)
    // The second pointer cannot move or end the first one's gesture.
    expect(g.move(pointer({ pointerId: 2, clientX: 100 }))).toBeNull()
    expect(g.end({ pointerId: 2 })).toEqual({ tap: 'none' })
    // And the first can still complete normally.
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'node', nodeId: 'n1', additive: false })
  })

  it('a non-primary pointer never starts a gesture', () => {
    const g = new GraphGesture()
    expect(g.begin(pointer({ isPrimary: false }), 'n1')).toBe(false)
    expect(g.active).toBe(false)
  })

  it('a cancelled gesture activates nothing', () => {
    const g = new GraphGesture()
    g.begin(pointer(), 'n1')
    expect(g.cancel()).toBe(true)
    expect(g.active).toBe(false)
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'none' })
  })

  it('cancelling a pan mid-drag stops moving with no tap', () => {
    const g = new GraphGesture()
    g.begin(pointer(), null)
    g.move(pointer({ clientX: 40 }))
    expect(g.cancel()).toBe(true)
    expect(g.move(pointer({ clientX: 80 }))).toBeNull()
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'none' })
  })

  it('cancel only clears the pointer that owns the gesture', () => {
    const g = new GraphGesture()
    g.begin(pointer({ pointerId: 7 }), 'n1')
    expect(g.cancel({ pointerId: 8 })).toBe(false)
    expect(g.active).toBe(true)
    expect(g.activePointerId).toBe(7)
  })

  it('an additive press is carried through to the tap', () => {
    const g = new GraphGesture()
    g.begin(pointer({ additive: true }), 'n1')
    expect(g.end({ pointerId: 1 })).toEqual({ tap: 'node', nodeId: 'n1', additive: true })
  })

  it('reports the node being dragged, and only while dragging', () => {
    const g = new GraphGesture()
    g.begin(pointer(), 'n1')
    expect(g.draggingNodeId).toBeNull()
    g.move(pointer({ clientX: TAP_SLOP_PX + 5 }))
    expect(g.draggingNodeId).toBe('n1')
    g.end({ pointerId: 1 })
    expect(g.draggingNodeId).toBeNull()
  })

  it('a pan does not report a dragged node', () => {
    const g = new GraphGesture()
    g.begin(pointer(), null)
    g.move(pointer({ clientX: 30 }))
    expect(g.draggingNodeId).toBeNull()
  })
})
