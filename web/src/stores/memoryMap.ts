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
  /** Epoch seconds from the server, ordering the recently-written entry points. */
  mtime: number
  /** Frontmatter `updated:` (YYYY-MM-DD) — when the facts were last verified. */
  updated: string
  /**
   * True when the note's age passes its type's staleness horizon, computed
   * server-side by the same detector the audit and daily curation consume.
   * Event types (logs, journals) never go stale and are never flagged.
   */
  stale: boolean
  /** Days since last verification; null when the note carries no usable date. */
  ageDays: number | null
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
 * labels — the sidebar legend names every coloured cluster, and hovering a
 * node names it — so the colour never has to carry identification alone.
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
   * the single biggest legibility win available from a toggle, but showing
   * *only* them is the to-do view for linking or deleting.
   */
  const orphanFilter = ref<'all' | 'hide' | 'only'>('all')
  // Backward-compat: existing UI/tests address `hideOrphans` as a boolean.
  const hideOrphans = computed({
    get: () => orphanFilter.value === 'hide',
    set: (v: boolean) => { orphanFilter.value = v ? 'hide' : 'all' },
  })
  /** 'category' is the note's own type; 'cluster' is detected community. */
  const colorMode = ref<'category' | 'cluster'>('category')
  /**
   * Which surface the memory page shows. The switcher itself lives in the
   * sidebar next to the workspace toggle, so this state is shared between
   * `ProjectSidebar` (the buttons) and `MemoryMapView` (the surfaces).
   */
  const view = ref<'graph' | 'list' | 'review'>('graph')
  // Bumped whenever something outside the canvas (the sidebar's "most
  // connected" list, a neighbor link) asks the canvas to pan/zoom onto a
  // node. The canvas owns camera state and only needs to watch this signal.
  //
  // `magnify` separates the two reasons to focus. A name in a sidebar list is
  // a note the user cannot see on the canvas, so the camera zooms in far
  // enough for its title to be painted; a click on a dot is a note they are
  // already looking at, and zooming there would throw away the overview on
  // every click.
  const focusSignal = reactive<{ id: string | null; seq: number; magnify: boolean }>(
    { id: null, seq: 0, magnify: true },
  )

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

  function passesFilters(n: MemoryGraphNode): boolean {
    if (!activeCats.has(catKeyFor(n))) return false
    if (search.value.trim() && !matchesSearch(n, search.value)) return false
    if (orphanFilter.value === 'hide' && n.degree === 0) return false
    if (orphanFilter.value === 'only' && n.degree !== 0) return false
    return true
  }

  const visibleNodes = computed(() => nodes.value.filter(passesFilters))
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
   * Notes whose facts have gone unverified past their type's horizon. Same
   * to-do-list shape as orphans: each one is worth re-checking, correcting,
   * or deleting — and the daily curation routine works from the same list,
   * so what shows here is exactly what that run will touch.
   */
  const staleNotes = computed(() =>
    nodes.value
      .filter(n => n.stale && activeCats.has(catKeyFor(n)))
      .sort((a, b) => (b.ageDays ?? 0) - (a.ageDays ?? 0)),
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

  /**
   * Compact age for list rows and the detail panel: "5mo" style, or an empty
   * string when the note carries no usable date at all.
   */
  function ageLabelOf(n: MemoryGraphNode): string {
    if (n.ageDays === null && !n.updated && !n.mtime) return ''
    const days = n.ageDays ?? Math.floor((Date.now() / 1000 - n.mtime) / 86400)
    if (!Number.isFinite(days)) return ''
    if (days < 1) return 'today'
    if (days < 30) return `${days}d`
    if (days < 365) return `${Math.floor(days / 30)}mo`
    const years = days / 365
    return `${years >= 2 ? Math.floor(years) : years.toFixed(1)}y`
  }

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

  /**
   * One snapshot per workspace, kept for the life of the session.
   *
   * The map used to be rebuilt from scratch on every visit: a full re-fetch
   * (the server reads and parses every note in every vault), a full skeleton,
   * and a full force-layout warmup — for a graph that had not changed since
   * the user left the page thirty seconds earlier. Snapshots hold the same
   * node objects the canvas mutates, so a workspace you have already looked
   * at comes back instantly, with its settled layout intact.
   */
  interface GraphSnapshot {
    nodes: MemoryGraphNode[]
    edges: MemoryGraphEdge[]
    signature: string
    /** The canvas has already run a layout warmup on these positions. */
    warm: boolean
  }
  const graphCache = new Map<string, GraphSnapshot>()
  const loadedWorkspace = ref('')
  /**
   * Ticket for the newest in-flight graph request; older responses are dropped.
   *
   * Every load races the workspace switcher (and the background revalidation
   * ensureGraph fires). A slow /api/vault/graph for the workspace the user has
   * just left used to publish anyway, overwriting `nodes`/`loadedWorkspace`
   * with the abandoned scope's graph — the map then showed the wrong vault
   * until something else forced a reload.
   */
  let graphRequestSeq = 0
  /** Whether the current positions are settled, i.e. the canvas can skip the
   * warmup and go straight to the short settle. */
  const graphIsWarm = ref(false)
  function markGraphWarm() {
    graphIsWarm.value = true
    const snap = graphCache.get(loadedWorkspace.value)
    if (snap) snap.warm = true
  }

  /** Cheap identity of a graph's content, to tell a no-op refresh from a real
   * change without diffing every field. */
  function signatureOf(ns: { id: string; updated: string; degree: number; stale: boolean }[], es: MemoryGraphEdge[]): string {
    const nodePart = ns.map(n => `${n.id}|${n.updated}|${n.degree}|${n.stale ? 1 : 0}`).join('\n')
    return `${ns.length}:${es.length}\n${nodePart}`
  }

  function adopt(workspace: string, snap: GraphSnapshot) {
    nodes.value = snap.nodes
    edges.value = snap.edges
    loadedWorkspace.value = workspace
    graphIsWarm.value = snap.warm
    loading.value = false
    loadError.value = ''
  }

  /**
   * Show the graph for `workspace`, fetching only when we have nothing to
   * show. A workspace already in the cache is adopted immediately and then
   * revalidated in the background, so the common case (leaving the page and
   * coming back) costs no skeleton and no re-layout.
   */
  async function ensureGraph(workspace: string) {
    const cached = graphCache.get(workspace)
    if (cached && cached.nodes.length) {
      if (loadedWorkspace.value !== workspace) adopt(workspace, cached)
      void loadGraph(workspace, { background: true })
      return
    }
    await loadGraph(workspace)
  }

  async function loadGraph(workspace: string, opts?: { background?: boolean }) {
    const background = opts?.background === true
    const seq = ++graphRequestSeq
    if (!background) loading.value = true
    loadError.value = ''
    try {
      const data = await api.get<{ nodes: any[]; edges: MemoryGraphEdge[] }>(
        `/api/vault/graph?workspace=${encodeURIComponent(workspace)}`,
      )
      // Superseded by a newer load (a workspace switch, or a later refresh):
      // this response describes a scope nobody is looking at, so it must not
      // touch the graph, the cache, or the spinner.
      if (seq !== graphRequestSeq) return
      const incoming: MemoryGraphNode[] = (data.nodes || []).map(n => ({
        ...n,
        tags: n.tags || [],
        aliases: n.aliases || [],
        description: n.description || '',
        mtime: typeof n.mtime === 'number' ? n.mtime : 0,
        updated: typeof n.updated === 'string' ? n.updated : '',
        stale: n.stale === true,
        ageDays: typeof n.age_days === 'number' ? n.age_days : null,
        x: (Math.random() - 0.5) * 800,
        y: (Math.random() - 0.5) * 800,
        vx: 0,
        vy: 0,
      }))
      const incomingEdges = (data.edges || []).filter(e => e.source !== e.target)
      const signature = signatureOf(incoming, incomingEdges)
      const previous = graphCache.get(workspace)
      // Nothing changed on disk: keep the layout the user is looking at rather
      // than swapping in identical data and re-settling it for no reason.
      if (previous && previous.signature === signature) {
        if (loadedWorkspace.value !== workspace) adopt(workspace, previous)
        return
      }
      // Carry positions across for notes we already had, so a refresh that adds
      // one note does not re-scatter the other five hundred.
      let carried = 0
      if (previous) {
        const prevById = new Map(previous.nodes.map(n => [n.id, n]))
        for (const n of incoming) {
          const old = prevById.get(n.id)
          if (!old) continue
          n.x = old.x
          n.y = old.y
          carried += 1
        }
      }
      const snap: GraphSnapshot = {
        nodes: incoming,
        edges: incomingEdges,
        signature,
        // Positions inherited wholesale are already settled; a graph that is
        // mostly new notes at random coordinates is not.
        warm: Boolean(previous?.warm) && carried >= incoming.length * 0.5,
      }
      graphCache.set(workspace, snap)
      nodes.value = snap.nodes
      edges.value = snap.edges
      loadedWorkspace.value = workspace
      graphIsWarm.value = snap.warm
      activeCats.clear()
      categoryList.value.forEach(c => activeCats.add(c.key))
      selectedId.value = null
      pathStart.value = null
      pathEnd.value = null
    } catch (err) {
      // A silent background refresh must not replace the graph on screen with
      // an error card; the data we are showing is still the data we had. Nor
      // may a superseded load report a failure for a scope nobody is on.
      if (!background && seq === graphRequestSeq) {
        loadError.value = err instanceof Error ? err.message : 'Failed to load the vault graph.'
      }
    } finally {
      // The newest load owns the spinner; clearing it from an older one would
      // drop the skeleton while its request is still in flight.
      if (!background && seq === graphRequestSeq) loading.value = false
    }
  }

  // The graph must reflect whichever workspace is active everywhere else in
  // the app (sidebar toggle, number-key shortcut, chat header) — not a
  // workspace choice private to this view. Reloading here, rather than
  // requiring every consumer to remember to watch it, is what keeps the
  // graph from going stale/blank on a workspace switch.
  const projectStore = useProjectStore()
  watch(() => projectStore.activeWorkspace, (ws) => { void ensureGraph(ws) })

  function setColorMode(mode: 'category' | 'cluster') {
    colorMode.value = mode
  }
  function toggleHideOrphans() {
    orphanFilter.value = orphanFilter.value === 'hide' ? 'all' : 'hide'
  }
  function toggleOnlyOrphans() {
    orphanFilter.value = orphanFilter.value === 'only' ? 'all' : 'only'
  }
  function setOrphanFilter(v: 'all' | 'hide' | 'only') {
    orphanFilter.value = v
  }
  /** Show only one cluster, the cluster-space equivalent of "only" on a category. */
  function isolateCluster(clusterId: number) {
    const cluster = clusterById.value.get(clusterId)
    if (!cluster) return
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
  /**
   * Select a node and ask the canvas to pan onto it.
   *
   * @param magnify Also zoom in far enough to read the note's title. Callers
   * that name the note on screen already (a canvas click) pass false.
   */
  function requestFocus(id: string, magnify = true) {
    selectedId.value = id
    focusSignal.id = id
    focusSignal.magnify = magnify
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
    // The cached snapshot holds the arrays we just replaced; leaving it stale
    // would resurrect the deleted note on the next visit to this workspace.
    const snap = graphCache.get(loadedWorkspace.value)
    if (snap) {
      snap.nodes = nodes.value
      snap.edges = edges.value
      snap.signature = signatureOf(nodes.value, edges.value)
    }
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
    // No magnification, though — the dot was already under the cursor.
    requestFocus(id, false)
  }

  return {
    nodes, edges, loading, loadError, search, activeCats, selectedId, pathStart, pathEnd, focusSignal,
    hideOrphans, orphanFilter, colorMode, view,
    nodesById, adjacency, categoryList, visibleNodes, visibleIds, visibleEdgeCount, orphanCount,
    mostConnected, selectedNode, pathIds, pathHint,
    clusters, clusterById, orphanNotes, bridgeNotes, recentNotes, staleNotes, ageLabelOf,
    clusterOf, clusterSlotOf, betweennessOf,
    neighborsOf, loadGraph, toggleCategory, isolateCategory, resetCategories,
    setColorMode, toggleHideOrphans, toggleOnlyOrphans, setOrphanFilter, isolateCluster,
    selectNode, requestFocus, resetPath, handleNodeClick, deleteNote,
    graphIsWarm, markGraphWarm, ensureGraph,
  }
})
