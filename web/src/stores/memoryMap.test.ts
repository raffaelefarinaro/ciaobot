// @vitest-environment jsdom

import { beforeEach, describe, expect, test } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { clusterColorFor, COLORED_CLUSTERS, useMemoryMapStore, type MemoryGraphNode } from './memoryMap'

beforeEach(() => {
  setActivePinia(createPinia())
})

function node(id: string, degree: number, extra: Partial<MemoryGraphNode> = {}): MemoryGraphNode {
  return {
    id,
    title: id,
    type: 'note',
    tags: [],
    aliases: [],
    description: '',
    workspace: 'work',
    degree,
    mtime: 0,
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
    ...extra,
  }
}

/** A path a — b — c — d, plus a disconnected orphan. */
function seedChain() {
  const mm = useMemoryMapStore()
  mm.nodes = [
    node('a', 1),
    node('b', 2),
    node('c', 2),
    node('d', 1),
    node('orphan', 0, { mtime: 999 }),
  ]
  mm.edges = [
    { source: 'a', target: 'b' },
    { source: 'b', target: 'c' },
    { source: 'c', target: 'd' },
  ]
  mm.resetCategories()
  return mm
}

describe('local scope', () => {
  test('depth 1 shows only the root and its direct neighbours', () => {
    const mm = seedChain()
    mm.setLocalRoot('b')
    mm.setLocalDepth(1)
    expect([...mm.visibleNodes.map(n => n.id)].sort()).toEqual(['a', 'b', 'c'])
  })

  test('depth 2 reaches two hops out', () => {
    const mm = seedChain()
    mm.setLocalRoot('a')
    mm.setLocalDepth(2)
    expect([...mm.visibleNodes.map(n => n.id)].sort()).toEqual(['a', 'b', 'c'])
  })

  test('overview scope ignores the root entirely', () => {
    const mm = seedChain()
    mm.setLocalRoot('a')
    mm.setScope('overview')
    expect(mm.visibleNodes).toHaveLength(5)
  })

  test('depth is clamped to a usable range', () => {
    const mm = seedChain()
    mm.setLocalDepth(99)
    expect(mm.localDepth).toBe(4)
    mm.setLocalDepth(0)
    expect(mm.localDepth).toBe(1)
  })

  test('the root stays visible even when its category is filtered out', () => {
    // Otherwise the view can end up empty while still claiming to be centred
    // on a real note, which reads as a broken graph.
    const mm = seedChain()
    mm.setLocalRoot('a')
    mm.activeCats.clear()
    expect(mm.visibleNodes.map(n => n.id)).toEqual(['a'])
  })

  test('clicking a node re-roots the local view but not the overview', () => {
    const mm = seedChain()
    mm.setLocalRoot('a')
    mm.handleNodeClick('c', false)
    expect(mm.localRoot).toBe('c')

    mm.setScope('overview')
    mm.handleNodeClick('a', false)
    expect(mm.localRoot).toBe('c')
  })

  test('shift-click still builds a path instead of re-rooting', () => {
    const mm = seedChain()
    mm.setLocalRoot('a')
    mm.handleNodeClick('d', true)
    expect(mm.localRoot).toBe('a')
    expect(mm.pathStart).toBe('d')
  })
})

describe('orphans', () => {
  test('hideOrphans drops unlinked notes from the graph', () => {
    const mm = seedChain()
    mm.setScope('overview')
    expect(mm.visibleNodes.map(n => n.id)).toContain('orphan')
    mm.toggleHideOrphans()
    expect(mm.visibleNodes.map(n => n.id)).not.toContain('orphan')
  })

  test('the unlinked list stays populated while they are hidden from the graph', () => {
    // The list is the actionable half of the feature: hiding them from the
    // layout must not also hide the work item.
    const mm = seedChain()
    mm.toggleHideOrphans()
    expect(mm.orphanNotes.map(n => n.id)).toEqual(['orphan'])
  })
})

describe('cluster palette', () => {
  test('only the validated slots get a hue; overflow shares the neutral', () => {
    const hues = new Set<string>()
    for (let slot = 0; slot < COLORED_CLUSTERS; slot++) hues.add(clusterColorFor(slot))
    expect(hues.size).toBe(COLORED_CLUSTERS)
    const overflow = clusterColorFor(COLORED_CLUSTERS)
    expect(hues.has(overflow)).toBe(false)
    expect(clusterColorFor(COLORED_CLUSTERS + 5)).toBe(overflow)
    expect(clusterColorFor(undefined)).toBe(overflow)
  })

  test('light and dark themes use different steps', () => {
    expect(clusterColorFor(0, true)).not.toBe(clusterColorFor(0, false))
  })
})
