import { defineStore } from 'pinia'
import { computed, reactive, ref, watch } from 'vue'
import { api } from '../lib/api'
import { useProjectStore } from './projects'

export interface MemoryGraphNode {
  id: string
  title: string
  type: string
  tags: string[]
  aliases: string[]
  description: string
  workspace: string
  degree: number
  // simulation state, owned by the canvas but persisted here so the graph
  // does not re-scatter every time the sidebar touches the store.
  x: number
  y: number
  vx: number
  vy: number
}
export interface MemoryGraphEdge { source: string; target: string }

export const MEMORY_TYPE_META: Record<string, { label: string; color: string }> = {
  'person-self': { label: 'You', color: '#eab676' },
  'person-family': { label: 'Family & partner', color: '#f2789f' },
  'person-friend': { label: 'Friends', color: '#f2a65a' },
  'person-colleague': { label: 'Colleagues', color: '#7dc4e4' },
  'person-external': { label: 'External contacts', color: '#9aa5b1' },
  'person-person': { label: 'Other people', color: '#b48ead' },
  project: { label: 'Projects', color: '#7aa2f7' },
  'project-log': { label: 'Project logs', color: '#5f7fd6' },
  resource: { label: 'Resources', color: '#38bdae' },
  reference: { label: 'References', color: '#e0af68' },
  note: { label: 'Notes', color: '#9099b2' },
  log: { label: 'Logs', color: '#6b7280' },
  idea: { label: 'Ideas', color: '#f7768e' },
  place: { label: 'Places', color: '#73daca' },
  plan: { label: 'Plans', color: '#7c82e0' },
  analysis: { label: 'Analysis', color: '#61dafb' },
  document: { label: 'Documents', color: '#c99b6a' },
  hub: { label: 'Workspace hubs', color: '#ffffff' },
  'skill-proposal': { label: 'Skill proposals', color: '#576079' },
}

function personSubtype(tags: string[]): string {
  const t = new Set(tags)
  if (t.has('self')) return 'self'
  if (t.has('family')) return 'family'
  if (t.has('friend')) return 'friend'
  if (t.has('customer') || t.has('external')) return 'external'
  if (t.has('scandit') || t.has('colleague')) return 'colleague'
  return 'person'
}
export function catKeyFor(n: { type: string; tags: string[] }): string {
  if (n.type === 'person') return 'person-' + personSubtype(n.tags)
  return n.type || 'note'
}
export function categoryColorFor(key: string): string {
  return MEMORY_TYPE_META[key]?.color || '#8892a6'
}
export function categoryLabelFor(n: { type: string; tags: string[] }): string {
  return MEMORY_TYPE_META[catKeyFor(n)]?.label || (n.type || 'note')
}

function matchesSearch(n: MemoryGraphNode, term: string): boolean {
  const t = term.toLowerCase()
  return (
    n.title.toLowerCase().includes(t) ||
    n.aliases.some(a => a.toLowerCase().includes(t)) ||
    n.tags.some(tag => tag.toLowerCase().includes(t))
  )
}

/**
 * Vault graph data and filter state for the Memory Map (`/memory`).
 *
 * Shared between `MemoryMapView` (the canvas/list surface) and
 * `ProjectSidebar` (the vault stats, search, categories, and path-finder
 * controls that live in the sidebar for memory mode) — the same split used
 * for schedules/loops via `useTaskStore`. The active workspace is not owned
 * here: it follows `useProjectStore().activeWorkspace`, the one switcher
 * shared with every other view, so the graph reloads on any workspace
 * change (sidebar toggle, number-key shortcut, etc.) regardless of which
 * component triggered it.
 */
export const useMemoryMapStore = defineStore('memoryMap', () => {
  const nodes = ref<MemoryGraphNode[]>([])
  const edges = ref<MemoryGraphEdge[]>([])
  const loading = ref(false)
  const loadError = ref('')
  const search = ref('')
  const activeCats = reactive(new Set<string>())
  const selectedId = ref<string | null>(null)
  const pathStart = ref<string | null>(null)
  const pathEnd = ref<string | null>(null)
  // Bumped whenever something outside the canvas (the sidebar's "most
  // connected" list, a neighbor link) asks the canvas to pan/zoom onto a
  // node. The canvas owns camera state and only needs to watch this signal.
  const focusSignal = reactive<{ id: string | null; seq: number }>({ id: null, seq: 0 })

  const nodesById = computed(() => {
    const map = new Map<string, MemoryGraphNode>()
    nodes.value.forEach(n => map.set(n.id, n))
    return map
  })
  const adjacency = computed(() => {
    const map = new Map<string, string[]>()
    nodes.value.forEach(n => map.set(n.id, []))
    edges.value.forEach(e => {
      map.get(e.source)?.push(e.target)
      map.get(e.target)?.push(e.source)
    })
    return map
  })

  const categoryList = computed(() => {
    const counts = new Map<string, number>()
    nodes.value.forEach(n => {
      const key = catKeyFor(n)
      counts.set(key, (counts.get(key) || 0) + 1)
    })
    return [...counts.entries()]
      .map(([key, count]) => ({ key, count, label: MEMORY_TYPE_META[key]?.label || key, color: categoryColorFor(key) }))
      .sort((a, b) => b.count - a.count)
  })

  const visibleNodes = computed(() =>
    nodes.value.filter(n => activeCats.has(catKeyFor(n)) && (!search.value.trim() || matchesSearch(n, search.value))),
  )
  const visibleIds = computed(() => new Set(visibleNodes.value.map(n => n.id)))
  const visibleEdgeCount = computed(
    () => edges.value.filter(e => visibleIds.value.has(e.source) && visibleIds.value.has(e.target)).length,
  )
  const orphanCount = computed(() => visibleNodes.value.filter(n => n.degree === 0).length)
  const mostConnected = computed(() =>
    [...visibleNodes.value].sort((a, b) => b.degree - a.degree).slice(0, 6).filter(n => n.degree > 0),
  )
  const selectedNode = computed(() => (selectedId.value ? nodesById.value.get(selectedId.value) || null : null))

  const pathIds = computed<Set<string>>(() => {
    if (!pathStart.value || !pathEnd.value) return new Set()
    const visited = new Map<string, string | null>([[pathStart.value, null]])
    const queue = [pathStart.value]
    while (queue.length) {
      const cur = queue.shift() as string
      if (cur === pathEnd.value) break
      for (const nb of adjacency.value.get(cur) || []) {
        if (!visited.has(nb)) {
          visited.set(nb, cur)
          queue.push(nb)
        }
      }
    }
    if (!visited.has(pathEnd.value)) return new Set()
    const chain: string[] = []
    let cur: string | null = pathEnd.value
    while (cur !== null) {
      chain.push(cur)
      cur = visited.get(cur) ?? null
    }
    return new Set(chain)
  })
  const pathHint = computed(() => {
    if (!pathStart.value) return 'Shift-click a note to start, then shift-click another to trace the shortest path between them.'
    if (!pathEnd.value) return `Start: ${nodesById.value.get(pathStart.value)?.title || pathStart.value}. Shift-click another note.`
    if (pathIds.value.size === 0) return 'No path found between those two notes.'
    return `${pathIds.value.size} notes on the path.`
  })

  function neighborsOf(id: string): MemoryGraphNode[] {
    return (adjacency.value.get(id) || []).map(nid => nodesById.value.get(nid)).filter(Boolean) as MemoryGraphNode[]
  }

  async function loadGraph(workspace: string) {
    loading.value = true
    loadError.value = ''
    try {
      const data = await api.get<{ nodes: any[]; edges: MemoryGraphEdge[] }>(
        `/api/vault/graph?workspace=${encodeURIComponent(workspace)}`,
      )
      nodes.value = (data.nodes || []).map(n => ({
        ...n,
        tags: n.tags || [],
        aliases: n.aliases || [],
        description: n.description || '',
        x: (Math.random() - 0.5) * 800,
        y: (Math.random() - 0.5) * 800,
        vx: 0,
        vy: 0,
      }))
      edges.value = (data.edges || []).filter(e => e.source !== e.target)
      activeCats.clear()
      categoryList.value.forEach(c => activeCats.add(c.key))
      selectedId.value = null
      pathStart.value = null
      pathEnd.value = null
    } catch (err) {
      loadError.value = err instanceof Error ? err.message : 'Failed to load the vault graph.'
    } finally {
      loading.value = false
    }
  }

  // The graph must reflect whichever workspace is active everywhere else in
  // the app (sidebar toggle, number-key shortcut, chat header) — not a
  // workspace choice private to this view. Reloading here, rather than
  // requiring every consumer to remember to watch it, is what keeps the
  // graph from going stale/blank on a workspace switch.
  const projectStore = useProjectStore()
  watch(() => projectStore.activeWorkspace, (ws) => { void loadGraph(ws) })

  function toggleCategory(key: string) {
    if (activeCats.has(key)) activeCats.delete(key)
    else activeCats.add(key)
  }
  function isolateCategory(key: string) {
    activeCats.clear()
    activeCats.add(key)
  }
  function resetCategories() {
    activeCats.clear()
    categoryList.value.forEach(c => activeCats.add(c.key))
  }
  function selectNode(id: string | null) {
    selectedId.value = id
  }
  /** Select a node and ask the canvas to pan/zoom onto it. */
  function requestFocus(id: string) {
    selectedId.value = id
    focusSignal.id = id
    focusSignal.seq += 1
  }
  function resetPath() {
    pathStart.value = null
    pathEnd.value = null
  }
  /**
   * Permanently delete a note. The backend strips dangling references from
   * every note that linked to it before removing the file, so we only need
   * to mirror that locally: drop the node, drop its edges, and repair the
   * `degree` of whichever neighbors lose an edge — a full `loadGraph` reload
   * would re-randomize every node's (x, y) and re-scatter the whole canvas
   * just to remove one.
   */
  async function deleteNote(id: string) {
    await api.del(`/api/vault/note?path=${encodeURIComponent(id)}`)
    const removedEdges = edges.value.filter(e => e.source === id || e.target === id)
    edges.value = edges.value.filter(e => e.source !== id && e.target !== id)
    nodes.value = nodes.value.filter(n => n.id !== id)
    for (const e of removedEdges) {
      const neighbor = nodesById.value.get(e.source === id ? e.target : e.source)
      if (neighbor) neighbor.degree = Math.max(0, neighbor.degree - 1)
    }
    if (selectedId.value === id) selectedId.value = null
    if (pathStart.value === id) pathStart.value = null
    if (pathEnd.value === id) pathEnd.value = null
  }

  function handleNodeClick(id: string, shiftKey: boolean) {
    if (shiftKey) {
      if (!pathStart.value) pathStart.value = id
      else if (!pathEnd.value) pathEnd.value = id
      else { pathStart.value = id; pathEnd.value = null }
      return
    }
    // Clicking a node directly on the canvas should feel the same as
    // clicking it from the sidebar or a linked-note link: it becomes
    // selected AND the camera centers on it, not just a highlight in place.
    requestFocus(id)
  }

  return {
    nodes, edges, loading, loadError, search, activeCats, selectedId, pathStart, pathEnd, focusSignal,
    nodesById, adjacency, categoryList, visibleNodes, visibleIds, visibleEdgeCount, orphanCount,
    mostConnected, selectedNode, pathIds, pathHint,
    neighborsOf, loadGraph, toggleCategory, isolateCategory, resetCategories,
    selectNode, requestFocus, resetPath, handleNodeClick, deleteNote,
  }
})
