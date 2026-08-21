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

describe('visibility', () => {
  test('the whole vault is visible by default — there is no local scope', () => {
    const mm = seedChain()
    expect(mm.visibleNodes).toHaveLength(5)
  })

  test('selecting a node never hides the rest of the vault', () => {
    // Connectivity to the selection is shown by highlighting, not filtering:
    // the canvas dims everything that is not the selected note or its direct
    // neighbours, but the nodes stay in the layout.
    const mm = seedChain()
    mm.handleNodeClick('b', false)
    expect(mm.selectedId).toBe('b')
    expect(mm.visibleNodes).toHaveLength(5)
  })

  test('clicking a node selects it and asks the canvas to focus it', () => {
    const mm = seedChain()
    mm.handleNodeClick('c', false)
    expect(mm.selectedId).toBe('c')
    expect(mm.focusSignal.id).toBe('c')
    expect(mm.focusSignal.seq).toBe(1)
  })

  test('shift-click builds a path instead of changing the selection', () => {
    const mm = seedChain()
    mm.handleNodeClick('a', false)
    mm.handleNodeClick('d', true)
    expect(mm.pathStart).toBe('d')
    expect(mm.selectedId).toBe('a')
  })
})

describe('orphans', () => {
  test('hideOrphans drops unlinked notes from the graph', () => {
    const mm = seedChain()
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
