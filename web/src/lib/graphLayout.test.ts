/**
 * The memory map's layout, asserted without a canvas.
 *
 * None of this was reachable before the maths left the component: every
 * property below would have needed a mounted `<canvas>`, a ResizeObserver, a
 * live store and a RAF loop to observe, and so none of it was covered.
 */

import { describe, expect, it } from 'vitest'
import {
  DEFAULT_SCALE,
  MAX_SPEED,
  MIN_GAP,
  clampScale,
  ellipsize,
  fitCameraFor,
  hexToRgba,
  hitTest,
  labelDegreeFloor,
  labelsVisible,
  nodeRadius,
  screenToWorld,
  stepSimulation,
  warmupStepsFor,
  worldToScreen,
  type SimEdge,
  type SimNode,
} from './graphLayout'

function node(over: Partial<SimNode> & { id: string }): SimNode {
  return { x: 0, y: 0, vx: 0, vy: 0, degree: 1, ...over }
}

function run(nodes: SimNode[], edges: SimEdge[], steps: number, cooling = 1): number {
  let last = 0
  for (let i = 0; i < steps; i++) last = stepSimulation(nodes, edges, cooling)
  return last
}

describe('stepSimulation', () => {
  it('separates two nearly-coincident nodes', () => {
    // 1/d^2 alone is too weak here; the short-range shove is what fixed
    // label-on-label pairs surviving an otherwise settled layout.
    const a = node({ id: 'a', x: 0, y: 0 })
    const b = node({ id: 'b', x: 0.5, y: 0.5 })
    run([a, b], [], 200)
    expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThan(MIN_GAP)
  })

  it('leaves two EXACTLY coincident nodes stuck, which is the known degenerate case', () => {
    // With dx and dy both zero the repulsion has no direction to act along, so
    // `(dx / d) * f` is zero however large `f` grows and the pair never
    // separates. Pinned rather than fixed: the store seeds positions from
    // `Math.random()`, so exact coincidence does not arise in practice, and
    // changing it here would be a behaviour change rather than a move.
    const a = node({ id: 'a', x: 0, y: 0 })
    const b = node({ id: 'b', x: 0, y: 0 })
    run([a, b], [], 200)
    expect(Math.hypot(a.x - b.x, a.y - b.y)).toBe(0)
  })

  it('pulls linked nodes toward the spring length, not on top of each other', () => {
    const a = node({ id: 'a', x: -600, y: 0 })
    const b = node({ id: 'b', x: 600, y: 0 })
    run([a, b], [{ source: 'a', target: 'b' }], 400)
    const d = Math.hypot(a.x - b.x, a.y - b.y)
    expect(d).toBeLessThan(1200)
    expect(d).toBeGreaterThan(MIN_GAP)
  })

  it('never lets a node move faster than the speed clamp', () => {
    // Uncapped, a step near a hub could overshoot so far that close-packed
    // nodes swung further apart each step instead of settling closer.
    const nodes = Array.from({ length: 12 }, (_, i) => node({ id: `n${i}`, x: 0, y: 0, degree: 40 }))
    for (let i = 0; i < 30; i++) stepSimulation(nodes, [], 1)
    for (const n of nodes) {
      expect(Math.hypot(n.vx, n.vy)).toBeLessThanOrEqual(MAX_SPEED + 1e-9)
    }
  })

  it('drives velocity to zero as cooling reaches zero', () => {
    // The whole point of the cooling schedule: convergence is bounded even for
    // a hub-heavy graph that would otherwise oscillate indefinitely.
    const nodes = Array.from({ length: 8 }, (_, i) => node({ id: `n${i}`, x: i * 3, y: i * -2, degree: 6 }))
    const edges: SimEdge[] = nodes.slice(1).map(n => ({ source: 'n0', target: n.id }))
    run(nodes, edges, 50)
    const speed = run(nodes, edges, 1, 0)
    expect(speed).toBe(0)
    for (const n of nodes) {
      // Math.abs so a velocity that cooled to -0 reads as settled.
      expect(Math.abs(n.vx)).toBe(0)
      expect(Math.abs(n.vy)).toBe(0)
    }
  })

  it('ignores an edge whose endpoint is not in the visible set', () => {
    // Filters hide nodes without rebuilding the edge list, so a dangling edge
    // is normal and must not throw or move anything.
    const a = node({ id: 'a', x: 10, y: 0 })
    expect(() => stepSimulation([a], [{ source: 'a', target: 'gone' }])).not.toThrow()
  })

  it('reports the fastest node speed, which is what the settle detector reads', () => {
    const a = node({ id: 'a', x: 0, y: 0 })
    const b = node({ id: 'b', x: 1, y: 0 })
    expect(stepSimulation([a, b], [])).toBeGreaterThan(0)
  })

  it('is a no-op on an empty graph', () => {
    expect(stepSimulation([], [])).toBe(0)
  })
})

describe('warmupStepsFor', () => {
  it('scales with node count between a floor and a cap', () => {
    expect(warmupStepsFor(0)).toBe(80)
    expect(warmupStepsFor(10)).toBe(80)
    expect(warmupStepsFor(100)).toBe(300)
    // Capped: an uncapped budget keeps the settling visibly running long after
    // it stops changing anything.
    expect(warmupStepsFor(5000)).toBe(400)
  })
})

describe('fitCameraFor', () => {
  it('falls back to the default framing with nothing to frame', () => {
    expect(fitCameraFor([], 800, 600)).toEqual({ x: 0, y: 0, scale: DEFAULT_SCALE })
    // A zero-sized viewport happens while the canvas is still being laid out.
    expect(fitCameraFor([node({ id: 'a' })], 0, 600).scale).toBe(DEFAULT_SCALE)
  })

  it('centres the graph, whatever its offset', () => {
    const nodes = [
      node({ id: 'a', x: 100, y: 100 }),
      node({ id: 'b', x: 300, y: 300 }),
    ]
    const cam = fitCameraFor(nodes, 800, 600)
    // The bounding-box centre must land at the viewport centre, which is what
    // worldToScreen maps (0,0) + camera offset to.
    const [sx, sy] = worldToScreen(200, 200, cam, 800, 600)
    expect(sx).toBeCloseTo(400)
    expect(sy).toBeCloseTo(300)
  })

  it('frames a wide graph by its limiting axis', () => {
    const wide = [node({ id: 'a', x: -1000, y: 0 }), node({ id: 'b', x: 1000, y: 0 })]
    const cam = fitCameraFor(wide, 800, 600)
    // Width is the binding constraint, and the margin keeps the extremes inside.
    const [sx] = worldToScreen(1000, 0, cam, 800, 600)
    expect(sx).toBeLessThan(800)
    expect(sx).toBeGreaterThan(400)
  })

  it('never returns a scale the camera would refuse', () => {
    const tiny = [node({ id: 'a', x: 0, y: 0 }), node({ id: 'b', x: 0.001, y: 0 })]
    expect(fitCameraFor(tiny, 800, 600).scale).toBe(clampScale(Number.POSITIVE_INFINITY))
    const huge = [node({ id: 'a', x: -1e7, y: 0 }), node({ id: 'b', x: 1e7, y: 0 })]
    expect(fitCameraFor(huge, 800, 600).scale).toBe(clampScale(0))
  })
})

describe('clampScale', () => {
  it('bounds the zoom range', () => {
    expect(clampScale(0.01)).toBe(0.15)
    expect(clampScale(99)).toBe(3)
    expect(clampScale(1)).toBe(1)
  })
})

describe('worldToScreen / screenToWorld', () => {
  it('round-trip', () => {
    const cam = { x: -120, y: 40, scale: 1.7 }
    const [sx, sy] = worldToScreen(37, -84, cam, 800, 600)
    const [wx, wy] = screenToWorld(sx, sy, cam, 800, 600)
    expect(wx).toBeCloseTo(37)
    expect(wy).toBeCloseTo(-84)
  })

  it('puts the world origin at the viewport centre when the camera is home', () => {
    expect(worldToScreen(0, 0, { x: 0, y: 0, scale: 1 }, 800, 600)).toEqual([400, 300])
  })
})

describe('nodeRadius', () => {
  it('grows with degree but saturates', () => {
    expect(nodeRadius({ degree: 0 })).toBeLessThan(nodeRadius({ degree: 5 }))
    expect(nodeRadius({ degree: 5 })).toBeLessThan(nodeRadius({ degree: 20 }))
    // A 65-link hub must not become a blob that swallows its neighbours.
    expect(nodeRadius({ degree: 1000 })).toBe(17)
  })
})

describe('hitTest', () => {
  it('returns the topmost node under the point', () => {
    // Walked back to front, so the node drawn last — visibly on top — wins.
    const under = node({ id: 'under', x: 0, y: 0 })
    const over = node({ id: 'over', x: 0, y: 0 })
    expect(hitTest([under, over], 0, 0)?.id).toBe('over')
  })

  it('misses outside the radius and hits just inside it', () => {
    const n = node({ id: 'a', x: 100, y: 100, degree: 1 })
    const r = nodeRadius(n)
    expect(hitTest([n], 100 + r + 2, 100)?.id).toBe('a')
    expect(hitTest([n], 100 + r + 40, 100)).toBeNull()
  })

  it('is null on an empty graph', () => {
    expect(hitTest([], 0, 0)).toBeNull()
  })
})

describe('label pressure', () => {
  it('exempts a sparse view from the zoom gate', () => {
    // A small filtered view has a low zoom ratio but acres of space; hiding
    // every title there would hide the only notes the user came to read.
    expect(labelsVisible(4, 0.5)).toBe(true)
    expect(labelDegreeFloor(4, 0.5)).toBe(0)
  })

  it('hides every label on a dense graph framed far out', () => {
    expect(labelsVisible(300, 1.0)).toBe(false)
    expect(labelsVisible(300, 1.6)).toBe(true)
  })

  it('relaxes the degree floor as the user zooms in', () => {
    expect(labelDegreeFloor(300, 1.6)).toBe(3)
    expect(labelDegreeFloor(300, 2.5)).toBe(1)
    expect(labelDegreeFloor(300, 3.5)).toBe(0)
  })
})

describe('ellipsize', () => {
  // One unit of width per character makes the binary search checkable by eye.
  const measure = (s: string) => s.length

  it('leaves text that already fits', () => {
    expect(ellipsize('short', 10, measure)).toBe('short')
    expect(ellipsize('exactly10!', 10, measure)).toBe('exactly10!')
  })

  it('truncates to the widest prefix that fits, with the ellipsis counted', () => {
    // 10 units of room, and the ellipsis costs one: 9 characters survive.
    expect(ellipsize('abcdefghijklmno', 10, measure)).toBe('abcdefghi…')
  })

  it('degrades to a bare ellipsis rather than overflowing', () => {
    expect(ellipsize('abcdef', 1, measure)).toBe('…')
  })

  it('handles a zero budget without looping', () => {
    expect(ellipsize('abc', 0, measure)).toBe('…')
  })
})

describe('hexToRgba', () => {
  it('converts with and without the hash', () => {
    expect(hexToRgba('#ff8000', 0.5)).toBe('rgba(255,128,0,0.5)')
    expect(hexToRgba('000000', 1)).toBe('rgba(0,0,0,1)')
  })
})
