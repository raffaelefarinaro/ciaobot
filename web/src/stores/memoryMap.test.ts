// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
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
    updated: '',
    stale: false,
    ageDays: null,
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

  test('a canvas click pans without magnifying — the dot was already visible', () => {
    const mm = seedChain()
    mm.handleNodeClick('c', false)
    expect(mm.focusSignal.magnify).toBe(false)
  })

  test('focusing from a sidebar list magnifies, since the note is off-view', () => {
    const mm = seedChain()
    mm.requestFocus('d')
    expect(mm.focusSignal.id).toBe('d')
    expect(mm.focusSignal.magnify).toBe(true)
    expect(mm.selectedId).toBe('d')
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

describe('staleness', () => {
  test('staleNotes ranks the oldest unverified note first', () => {
    const mm = useMemoryMapStore()
    mm.nodes = [
      node('mid', 1, { stale: true, ageDays: 100 }),
      node('fresh', 0),
      node('old', 1, { stale: true, ageDays: 400 }),
    ]
    mm.edges = []
    mm.resetCategories()
    expect(mm.staleNotes.map(n => n.id)).toEqual(['old', 'mid'])
    expect(mm.staleNotes).not.toContain(mm.nodesById.get('fresh'))
  })

  test('ageLabelOf stays quiet about notes with no usable date', () => {
    const mm = useMemoryMapStore()
    expect(mm.ageLabelOf(node('a', 0, { ageDays: 400 }))).toBe('1.1y')
    expect(mm.ageLabelOf(node('b', 0, { ageDays: 45 }))).toBe('1mo')
    expect(mm.ageLabelOf(node('c', 0, { ageDays: 3 }))).toBe('3d')
    expect(mm.ageLabelOf(node('d', 0, { ageDays: null, mtime: 0 }))).toBe('')
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


describe('graph snapshots', () => {
  /** One fetched note per id, with the shape the server actually sends. */
  function payload(ids: string[]) {
    return {
      nodes: ids.map(id => ({ id, title: id, type: 'note', tags: [], aliases: [], description: '', workspace: 'work', degree: 0, mtime: 1, updated: '2026-01-01' })),
      edges: [],
    }
  }

  beforeEach(() => {
    vi.mocked(api.get).mockReset()
  })

  test('a second visit to the same workspace paints from cache and revalidates once', async () => {
    vi.mocked(api.get).mockResolvedValue(payload(['a', 'b']))
    const mm = useMemoryMapStore()
    await mm.ensureGraph('work')
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(mm.nodes).toHaveLength(2)

    // Pretend the canvas has laid this out, and that the user moved a node.
    mm.markGraphWarm()
    mm.nodes[0].x = 123
    await mm.ensureGraph('work')

    // Adopted without a skeleton, and the settled position survived the
    // background refresh because the content signature was unchanged.
    expect(mm.loading).toBe(false)
    expect(mm.graphIsWarm).toBe(true)
    expect(mm.nodes[0].x).toBe(123)
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  test('a changed vault replaces the graph but keeps the positions it can', async () => {
    vi.mocked(api.get).mockResolvedValue(payload(['a', 'b']))
    const mm = useMemoryMapStore()
    await mm.ensureGraph('work')
    mm.markGraphWarm()
    mm.nodes[0].x = 77

    vi.mocked(api.get).mockResolvedValue(payload(['a', 'b', 'c']))
    await mm.loadGraph('work')
    expect(mm.nodes.map(n => n.id)).toEqual(['a', 'b', 'c'])
    expect(mm.nodes[0].x).toBe(77)
  })

  test('a failed background refresh leaves the visible graph and shows no error', async () => {
    vi.mocked(api.get).mockResolvedValue(payload(['a']))
    const mm = useMemoryMapStore()
    await mm.ensureGraph('work')

    vi.mocked(api.get).mockRejectedValue(new Error('offline'))
    await mm.loadGraph('work', { background: true })
    expect(mm.loadError).toBe('')
    expect(mm.nodes).toHaveLength(1)
  })

  test('a failed first load does surface the error', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('offline'))
    const mm = useMemoryMapStore()
    await mm.ensureGraph('work')
    expect(mm.loadError).toBe('offline')
  })

  test('a delete survives the next visit, cache included', async () => {
    vi.mocked(api.get).mockResolvedValue(payload(['a', 'b']))
    const mm = useMemoryMapStore()
    await mm.ensureGraph('work')
    vi.mocked(api.del).mockResolvedValue(undefined as never)
    await mm.deleteNote('a')
    expect(mm.nodes.map(n => n.id)).toEqual(['b'])

    // The snapshot the next visit adopts is the post-delete one, so the note
    // does not flash back on screen before the refresh lands.
    vi.mocked(api.get).mockResolvedValue(payload(['b']))
    await mm.ensureGraph('work')
    expect(mm.nodes.map(n => n.id)).toEqual(['b'])
  })
})
