import { defineStore } from 'pinia'
import { computed, reactive, ref, watch } from 'vue'
import { api } from '../lib/api'
import { analyzeVault, type Cluster } from '../lib/vaultAnalysis'
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
  /** Epoch seconds from the server, used to seed the local view. */
  mtime: number
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

/**
 * Cluster colours. Four hues and no more, plus a neutral for the overflow.
 *
 * A graph canvas is an all-pairs surface — any cluster can end up touching any
 * other — so the palette has to stay legible for *every* pair, not just
 * neighbouring ones in a legend. Running the eight-hue reference categorical
 * palette through the validator under `--pairs all` fails (worst pair CVD
 * dE 1.6); a brute-force search over its slots found four to be the largest
 * subset that clears every gate in both themes. The two residual WARNs (dark
 * CVD dE 6.9, light contrast on yellow/magenta) are both relieved by direct
 * labels, which is why cluster labels on the canvas and the sidebar legend are
 * not optional decoration here — they are what makes the colour legal.
 *
 * Slots are assigned by cluster size and never cycled: a fifth cluster takes
 * the neutral, it does not get an invented hue.
 */
export const CLUSTER_PALETTE = {
  dark: ['#3987e5', '#c98500', '#d55181', '#008300'],
  light: ['#2a78d6', '#eda100', '#e87ba4', '#008300'],
}
export const CLUSTER_OTHER_COLOR = { dark: '#6b7280', light: '#8e90a8' }
export const COLORED_CLUSTERS = CLUSTER_PALETTE.dark.length

export function clusterColorFor(slot: number | undefined, light = false): string {
  const theme = light ? 'light' : 'dark'
  if (slot === undefined || slot < 0 || slot >= COLORED_CLUSTERS) return CLUSTER_OTHER_COLOR[theme]
  return CLUSTER_PALETTE[theme][slot]
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
  /**
   * Orphans are 21% of a real vault and have no edges, so in the layout they
   * only feel centering and repulsion — they push the connected structure
   * apart while carrying no relational information themselves. Hiding them is
   * the single biggest legibility win available from a toggle.
   */
  const hideOrphans = ref(false)
  /** 'category' is the note's own type; 'cluster' is detected community. */
  const colorMode = ref<'category' | 'cluster'>('category')
  /**
   * 'overview' is the whole (filtered) vault; 'local' is a neighbourhood
   * around one note. Local is the default because a 300-node global view is a
   * hairball, while a depth-2 neighbourhood is the thing people actually read.
   */
  const scope = ref<'overview' | 'local'>('local')
  const localRoot = ref<string | null>(null)
  const localDepth = ref(2)
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

  /**
   * Community detection and betweenness over the FULL graph, not the filtered
   * view: cluster colours and "bridge note" rankings must not change when a
   * category is toggled off, or the encoding would be describing the filter
   * rather than the vault. Recomputes only when the graph data itself changes.
   */
  const analysis = computed(() => analyzeVault(nodes.value, edges.value))
  const clusters = computed<Cluster[]>(() => analysis.value.clusters)
  const clusterById = computed(() => new Map(clusters.value.map(c => [c.id, c])))
  function clusterOf(id: string): Cluster | null {
    const cid = analysis.value.communityOf.get(id)
    return cid === undefined ? null : clusterById.value.get(cid) || null
  }
  /** Slot drives the colour; undefined means "not in a real cluster". */
  function clusterSlotOf(id: string): number | undefined {
    return clusterOf(id)?.slot
  }

  /**
   * Notes within `localDepth` hops of the local root. The root is always
   * included even when a category filter would exclude it, so the view can
   * never end up empty while pointing at a real note.
   */
  const localIds = computed<Set<string>>(() => {
    const root = localRoot.value
    if (!root || !nodesById.value.has(root)) return new Set()
    const seen = new Set([root])
    let frontier = [root]
    for (let d = 0; d < localDepth.value; d++) {
      const next: string[] = []
      for (const id of frontier) {
        for (const nb of adjacency.value.get(id) || []) {
          if (seen.has(nb)) continue
          seen.add(nb)
          next.push(nb)
        }
      }
      if (!next.length) break
      frontier = next
    }
    return seen
  })

  function passesFilters(n: MemoryGraphNode): boolean {
    if (!activeCats.has(catKeyFor(n))) return false
    if (search.value.trim() && !matchesSearch(n, search.value)) return false
    if (hideOrphans.value && n.degree === 0) return false
    return true
  }

  const visibleNodes = computed(() => {
    if (scope.value === 'local' && localRoot.value) {
      const ids = localIds.value
      return nodes.value.filter(n => ids.has(n.id) && (n.id === localRoot.value || passesFilters(n)))
    }
    return nodes.value.filter(passesFilters)
  })
  const visibleIds = computed(() => new Set(visibleNodes.value.map(n => n.id)))
  const visibleEdgeCount = computed(
    () => edges.value.filter(e => visibleIds.value.has(e.source) && visibleIds.value.has(e.target)).length,
  )
  const orphanCount = computed(() => visibleNodes.value.filter(n => n.degree === 0).length)
  const mostConnected = computed(() =>
    [...visibleNodes.value].sort((a, b) => b.degree - a.degree).slice(0, 6).filter(n => n.degree > 0),
  )
  /**
   * Notes nothing links to. Listed rather than merely counted, because in a
   * vault this is a to-do list: each one is either worth linking up or worth
   * deleting, and neither is actionable from a number alone.
   */
  const orphanNotes = computed(() =>
    nodes.value
      .filter(n => n.degree === 0 && activeCats.has(catKeyFor(n)))
      .sort((a, b) => b.mtime - a.mtime),
  )
  /**
   * High betweenness with modest degree = a note that sits *between* clusters
   * rather than at the centre of one. Those are the notes whose deletion would
   * actually fragment the vault, which a degree ranking never surfaces.
   */
  const bridgeNotes = computed(() => {
    const scores = analysis.value.betweenness
    return [...visibleNodes.value]
      .filter(n => n.degree > 1 && (scores.get(n.id) || 0) > 0)
      .sort((a, b) => (scores.get(b.id) || 0) - (scores.get(a.id) || 0))
      .slice(0, 6)
  })
  function betweennessOf(id: string): number {
    return analysis.value.betweenness.get(id) || 0
  }
  /** Entry points for the local view, most recently written first. */
  const recentNotes = computed(() => [...nodes.value].sort((a, b) => b.mtime - a.mtime).slice(0, 6))
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
        mtime: typeof n.mtime === 'number' ? n.mtime : 0,
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
      localRoot.value = defaultLocalRoot()
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

  /**
   * How many recent notes to consider when picking an entry point. Pure
   * recency picks dead ends: on a real vault the newest note was a
   * single-link log entry, so the opening view was four nodes and told the
   * user nothing. Taking the best-connected note out of a recent window keeps
   * the "where I was working" intent while landing somewhere with structure
   * around it.
   */
  const ROOT_RECENCY_WINDOW = 15
  /**
   * Falls back to the biggest hub overall when the server sent no mtimes
   * (older backend), and to nothing at all for an empty vault.
   */
  function defaultLocalRoot(): string | null {
    if (!nodes.value.length) return null
    const byRecency = [...nodes.value].sort((a, b) => b.mtime - a.mtime)
    const window = byRecency[0]?.mtime ? byRecency.slice(0, ROOT_RECENCY_WINDOW) : nodes.value
    const best = [...window].sort((a, b) => b.degree - a.degree || b.mtime - a.mtime)[0]
    return best?.id || null
  }
  function setScope(next: 'overview' | 'local') {
    scope.value = next
    if (next === 'local' && !localRoot.value) localRoot.value = selectedId.value || defaultLocalRoot()
  }
  /** Re-root the local view, switching into it if the user was in overview. */
  function setLocalRoot(id: string) {
    localRoot.value = id
    scope.value = 'local'
  }
  function setLocalDepth(depth: number) {
    localDepth.value = Math.max(1, Math.min(4, Math.round(depth)))
  }
  function setColorMode(mode: 'category' | 'cluster') {
    colorMode.value = mode
  }
  function toggleHideOrphans() {
    hideOrphans.value = !hideOrphans.value
  }
  /** Show only one cluster, the cluster-space equivalent of "only" on a category. */
  function isolateCluster(clusterId: number) {
    const cluster = clusterById.value.get(clusterId)
    if (!cluster) return
    scope.value = 'overview'
    search.value = ''
    activeCats.clear()
    for (const id of cluster.memberIds) {
      const node = nodesById.value.get(id)
      if (node) activeCats.add(catKeyFor(node))
    }
    // Category filters cannot express "these exact notes", so isolating a
    // cluster centres it via focus rather than pretending to filter to it.
    requestFocus(cluster.memberIds[0])
  }
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
    // In local scope a click also walks the view to that note, which is what
    // makes the neighbourhood browsable instead of a dead end.
    if (scope.value === 'local') localRoot.value = id
    requestFocus(id)
  }

  return {
    nodes, edges, loading, loadError, search, activeCats, selectedId, pathStart, pathEnd, focusSignal,
    hideOrphans, colorMode, scope, localRoot, localDepth,
    nodesById, adjacency, categoryList, visibleNodes, visibleIds, visibleEdgeCount, orphanCount,
    mostConnected, selectedNode, pathIds, pathHint,
    clusters, clusterById, localIds, orphanNotes, bridgeNotes, recentNotes,
    clusterOf, clusterSlotOf, betweennessOf,
    neighborsOf, loadGraph, toggleCategory, isolateCategory, resetCategories,
    setScope, setLocalRoot, setLocalDepth, setColorMode, toggleHideOrphans, isolateCluster,
    defaultLocalRoot,
    selectNode, requestFocus, resetPath, handleNodeClick, deleteNote,
  }
})
