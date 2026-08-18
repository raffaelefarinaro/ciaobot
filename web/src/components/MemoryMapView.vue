<template>
  <div class="memory-map">
    <PaneHeader page-tag="memory" @open-sidebar="emit('open-sidebar')">
      <template #actions>
        <div v-if="workspaceOptions.length > 1" class="mm-workspace-toggle">
          <button
            v-for="ws in workspaceOptions"
            :key="ws.name"
            type="button"
            class="touch-hit"
            :class="{ active: activeWorkspace === ws.name }"
            :data-workspace-color="colorForWorkspace(ws)"
            @click="selectWorkspace(ws.name)"
          >
            {{ workspaceLabel(ws.name) }}
          </button>
        </div>
        <div class="mm-seg">
          <button type="button" :class="{ active: view === 'graph' }" @click="view = 'graph'">Graph</button>
          <button type="button" :class="{ active: view === 'list' }" @click="view = 'list'">List</button>
        </div>
      </template>
    </PaneHeader>

    <div class="mm-body">
      <aside class="mm-sidebar">
        <h3>Vault</h3>
        <div class="mm-stat-grid">
          <div class="mm-stat"><div class="n">{{ visibleNodes.length }}</div><div class="l">notes shown</div></div>
          <div class="mm-stat"><div class="n">{{ visibleEdgeCount }}</div><div class="l">links</div></div>
          <div class="mm-stat"><div class="n">{{ orphanCount }}</div><div class="l">orphaned</div></div>
          <div class="mm-stat"><div class="n">{{ nodes.length }}</div><div class="l">total</div></div>
        </div>

        <div class="mm-search">
          <input v-model="search" type="text" placeholder="Search notes, tags…" autocomplete="off" />
        </div>

        <div class="mm-row-between">
          <h3>Categories</h3>
          <button type="button" class="mm-link" @click="resetCategories">reset</button>
        </div>
        <div class="mm-chip-row">
          <div
            v-for="cat in categoryList"
            :key="cat.key"
            class="mm-chip"
            :class="{ off: !activeCats.has(cat.key) }"
            @click="toggleCategory(cat.key)"
          >
            <span class="dot" :style="{ background: cat.color }" />
            <span class="label">{{ cat.label }}</span>
            <span class="cnt">{{ cat.count }}</span>
            <button type="button" class="only" @click.stop="isolateCategory(cat.key)">only</button>
          </div>
        </div>

        <template v-if="mostConnected.length">
          <h3>Most connected</h3>
          <div class="mm-link-list">
            <div v-for="n in mostConnected" :key="n.id" class="mm-link-item" @click="focusNode(n.id)">
              <span class="dot" :style="{ background: colorForNode(n) }" />
              <span class="label">{{ n.title }}</span>
              <span class="cnt">{{ n.degree }}</span>
            </div>
          </div>
        </template>

        <h3>Path finder</h3>
        <p class="mm-hint">
          {{ pathHint }}
        </p>
        <button v-if="pathStart || pathEnd" type="button" class="mm-link" @click="resetPath">clear path</button>
      </aside>

      <div v-if="loading" class="mm-empty">Loading vault graph…</div>
      <div v-else-if="loadError" class="mm-empty">{{ loadError }}</div>
      <div v-else-if="view === 'graph'" class="mm-canvas-wrap" ref="canvasWrap">
        <canvas
          ref="canvasEl"
          @mousedown="onMouseDown"
          @wheel.prevent="onWheel"
        />
        <div class="mm-zoom-controls">
          <button type="button" class="btn-icon touch-hit" @click="zoom(1.25)">+</button>
          <button type="button" class="btn-icon touch-hit" @click="zoom(0.8)">−</button>
          <button type="button" class="btn-icon touch-hit" @click="resetCamera">⤢</button>
        </div>
        <div class="mm-hint-overlay">
          Drag to pan · scroll to zoom · drag a node to move it · click for details · shift-click two notes to find a path
        </div>
      </div>
      <div v-else class="mm-list-wrap">
        <table>
          <thead>
            <tr>
              <th @click="setSort('title')">Name</th>
              <th @click="setSort('type')">Type</th>
              <th>Tags</th>
              <th @click="setSort('degree')">Links</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="n in sortedVisibleNodes" :key="n.id" @click="selectNode(n.id)">
              <td><span class="dot" :style="{ background: colorForNode(n) }" />{{ n.title }}</td>
              <td class="muted">{{ categoryLabelFor(n) }}</td>
              <td>
                <span v-for="t in n.tags.slice(0, 4)" :key="t" class="tag-mini">{{ t }}</span>
              </td>
              <td>{{ n.degree }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <aside class="mm-detail">
        <div v-if="!selectedNode" class="mm-empty-detail">
          Select a note to see its tags, description, and what it links to.
        </div>
        <template v-else>
          <div class="mm-detail-type">{{ categoryLabelFor(selectedNode) }}</div>
          <div class="mm-detail-title">{{ selectedNode.title }}</div>
          <div v-if="selectedNode.description" class="mm-detail-desc">{{ selectedNode.description }}</div>
          <div class="mm-detail-path">{{ selectedNode.id }}</div>

          <div v-if="selectedNode.tags.length" class="mm-detail-section">
            <h4>Tags</h4>
            <span v-for="t in selectedNode.tags" :key="t" class="pill">{{ t }}</span>
          </div>
          <div v-if="selectedNode.aliases.length" class="mm-detail-section">
            <h4>Aliases</h4>
            <span v-for="a in selectedNode.aliases" :key="a" class="pill">{{ a }}</span>
          </div>

          <div class="mm-detail-section">
            <h4>Linked notes ({{ neighborsOf(selectedNode.id).length }})</h4>
            <div v-if="!neighborsOf(selectedNode.id).length" class="mm-hint">No links found — orphaned note.</div>
            <div
              v-for="nb in neighborsOf(selectedNode.id)"
              :key="nb.id"
              class="mm-link-item"
              @click="focusNode(nb.id)"
            >
              <span class="dot" :style="{ background: colorForNode(nb) }" />
              <span class="label">{{ nb.title }}</span>
            </div>
          </div>
        </template>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import PaneHeader from './PaneHeader.vue'
import { useProjectStore } from '../stores/projects'
import { api } from '../lib/api'
import { workspaceLabel } from '../lib/workspaceLabel'
import { colorForWorkspace } from '../lib/workspaceColors'

const emit = defineEmits<{ 'open-sidebar': [] }>()

interface GraphNode {
  id: string
  title: string
  type: string
  tags: string[]
  aliases: string[]
  description: string
  workspace: string
  degree: number
  // simulation state
  x: number
  y: number
  vx: number
  vy: number
}
interface GraphEdge { source: string; target: string }

const store = useProjectStore()
const workspaceOptions = computed(() => store.workspaceOptions)
const activeWorkspace = ref(store.activeWorkspace || 'personal')

function selectWorkspace(name: string) {
  if (activeWorkspace.value === name) return
  activeWorkspace.value = name
  loadGraph()
}

function colorForNode(n: GraphNode): string {
  return categoryColor(catKey(n))
}

// ---------- data load ----------
const nodes = ref<GraphNode[]>([])
const edges = ref<GraphEdge[]>([])
const nodesById = computed(() => {
  const map = new Map<string, GraphNode>()
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
const loading = ref(false)
const loadError = ref('')

async function loadGraph() {
  loading.value = true
  loadError.value = ''
  try {
    const data = await api.get<{ nodes: any[]; edges: GraphEdge[] }>(
      `/api/vault/graph?workspace=${encodeURIComponent(activeWorkspace.value)}`,
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
    resetPath()
    resetCamera()
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : 'Failed to load the vault graph.'
  } finally {
    loading.value = false
  }
}

// ---------- categories ----------
const TYPE_META: Record<string, { label: string; color: string }> = {
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
function catKey(n: GraphNode): string {
  if (n.type === 'person') return 'person-' + personSubtype(n.tags)
  return n.type || 'note'
}
function categoryColor(key: string): string {
  return TYPE_META[key]?.color || '#8892a6'
}
function categoryLabelFor(n: GraphNode): string {
  return TYPE_META[catKey(n)]?.label || (n.type || 'note')
}

const activeCats = reactive(new Set<string>())
const categoryList = computed(() => {
  const counts = new Map<string, number>()
  nodes.value.forEach(n => {
    const key = catKey(n)
    counts.set(key, (counts.get(key) || 0) + 1)
  })
  return [...counts.entries()]
    .map(([key, count]) => ({ key, count, label: TYPE_META[key]?.label || key, color: categoryColor(key) }))
    .sort((a, b) => b.count - a.count)
})

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

// ---------- search / filtering ----------
const search = ref('')
function matchesSearch(n: GraphNode, term: string): boolean {
  const t = term.toLowerCase()
  return (
    n.title.toLowerCase().includes(t) ||
    n.aliases.some(a => a.toLowerCase().includes(t)) ||
    n.tags.some(tag => tag.toLowerCase().includes(t))
  )
}
const visibleNodes = computed(() =>
  nodes.value.filter(n => activeCats.has(catKey(n)) && (!search.value.trim() || matchesSearch(n, search.value))),
)
const visibleIds = computed(() => new Set(visibleNodes.value.map(n => n.id)))
const visibleEdgeCount = computed(
  () => edges.value.filter(e => visibleIds.value.has(e.source) && visibleIds.value.has(e.target)).length,
)
const orphanCount = computed(() => visibleNodes.value.filter(n => n.degree === 0).length)
const mostConnected = computed(() =>
  [...visibleNodes.value].sort((a, b) => b.degree - a.degree).slice(0, 6).filter(n => n.degree > 0),
)

function neighborsOf(id: string): GraphNode[] {
  return (adjacency.value.get(id) || []).map(nid => nodesById.value.get(nid)).filter(Boolean) as GraphNode[]
}

// ---------- selection / detail panel ----------
const selectedId = ref<string | null>(null)
const selectedNode = computed(() => (selectedId.value ? nodesById.value.get(selectedId.value) || null : null))
function selectNode(id: string | null) {
  selectedId.value = id
}
function focusNode(id: string) {
  selectedId.value = id
  const n = nodesById.value.get(id)
  if (n) {
    camera.x = -n.x * camera.scale
    camera.y = -n.y * camera.scale
  }
}

// ---------- path finder ----------
const pathStart = ref<string | null>(null)
const pathEnd = ref<string | null>(null)
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
function resetPath() {
  pathStart.value = null
  pathEnd.value = null
}
function handleNodeClick(id: string, shiftKey: boolean) {
  if (shiftKey) {
    if (!pathStart.value) pathStart.value = id
    else if (!pathEnd.value) pathEnd.value = id
    else { pathStart.value = id; pathEnd.value = null }
    return
  }
  selectNode(id)
}

// ---------- canvas force layout ----------
const canvasEl = ref<HTMLCanvasElement | null>(null)
const canvasWrap = ref<HTMLDivElement | null>(null)
let ctx: CanvasRenderingContext2D | null = null
let rafId = 0
let ro: ResizeObserver | null = null
let W = 0
let H = 0
const camera = reactive({ x: 0, y: 0, scale: 1 })
const dpr = window.devicePixelRatio || 1

function resizeCanvas() {
  if (!canvasEl.value || !canvasWrap.value) return
  const w = canvasWrap.value.clientWidth
  const h = canvasWrap.value.clientHeight
  W = canvasEl.value.width = w * dpr
  H = canvasEl.value.height = h * dpr
  canvasEl.value.style.width = w + 'px'
  canvasEl.value.style.height = h + 'px'
}
function resetCamera() {
  camera.x = 0
  camera.y = 0
  camera.scale = 0.55
}
function zoom(factor: number) {
  camera.scale = Math.max(0.15, Math.min(3, camera.scale * factor))
}
function worldToScreen(x: number, y: number): [number, number] {
  return [x * camera.scale + W / 2 + camera.x, y * camera.scale + H / 2 + camera.y]
}
function screenToWorld(sx: number, sy: number): [number, number] {
  return [(sx - W / 2 - camera.x) / camera.scale, (sy - H / 2 - camera.y) / camera.scale]
}
function nodeRadius(n: GraphNode): number {
  return 4 + Math.min(10, Math.sqrt(n.degree + 1) * 2.4)
}
function hexToRgba(hex: string, a: number): string {
  const v = hex.replace('#', '')
  const r = parseInt(v.substring(0, 2), 16)
  const g = parseInt(v.substring(2, 4), 16)
  const b = parseInt(v.substring(4, 6), 16)
  return `rgba(${r},${g},${b},${a})`
}

function stepSimulation() {
  const vis = visibleNodes.value
  const REPEL = 2600
  const SPRING = 0.02
  const SPRING_LEN = 90
  const CENTER = 0.0025
  const DAMP = 0.85
  const forces = new Map<string, { fx: number; fy: number }>()
  for (let i = 0; i < vis.length; i++) {
    const a = vis[i]
    let fx = 0
    let fy = 0
    for (let j = 0; j < vis.length; j++) {
      if (i === j) continue
      const b = vis[j]
      let dx = a.x - b.x
      let dy = a.y - b.y
      let d2 = dx * dx + dy * dy
      if (d2 < 0.01) d2 = 0.01
      const d = Math.sqrt(d2)
      const f = REPEL / d2
      fx += (dx / d) * f
      fy += (dy / d) * f
    }
    fx += -a.x * CENTER
    fy += -a.y * CENTER
    forces.set(a.id, { fx, fy })
  }
  const visSet = visibleIds.value
  edges.value.forEach(e => {
    if (!visSet.has(e.source) || !visSet.has(e.target)) return
    const a = nodesById.value.get(e.source)
    const b = nodesById.value.get(e.target)
    if (!a || !b) return
    const dx = b.x - a.x
    const dy = b.y - a.y
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01
    const stretch = (d - SPRING_LEN) * SPRING
    const fx = (dx / d) * stretch
    const fy = (dy / d) * stretch
    const fa = forces.get(a.id)
    const fb = forces.get(b.id)
    if (fa) { fa.fx += fx; fa.fy += fy }
    if (fb) { fb.fx -= fx; fb.fy -= fy }
  })
  vis.forEach(n => {
    const f = forces.get(n.id)
    if (!f) return
    n.vx = (n.vx + f.fx) * DAMP
    n.vy = (n.vy + f.fy) * DAMP
    n.x += n.vx
    n.y += n.vy
  })
}

function draw() {
  if (!ctx) return
  ctx.clearRect(0, 0, W, H)
  const vis = visibleNodes.value
  const visSet = visibleIds.value
  const highlightSet = pathIds.value.size
    ? pathIds.value
    : selectedId.value
      ? new Set([selectedId.value, ...(adjacency.value.get(selectedId.value) || [])])
      : null

  ctx.lineWidth = dpr
  edges.value.forEach(e => {
    if (!visSet.has(e.source) || !visSet.has(e.target)) return
    const a = nodesById.value.get(e.source)
    const b = nodesById.value.get(e.target)
    if (!a || !b) return
    const onPath = pathIds.value.has(e.source) && pathIds.value.has(e.target)
    const dim = highlightSet && !(highlightSet.has(e.source) && highlightSet.has(e.target))
    const [ax, ay] = worldToScreen(a.x, a.y)
    const [bx, by] = worldToScreen(b.x, b.y)
    ctx!.strokeStyle = onPath
      ? '#ffd166'
      : dim
        ? 'rgba(120,126,150,0.08)'
        : 'rgba(150,160,190,0.35)'
    ctx!.lineWidth = onPath ? 2.5 * dpr : dpr
    ctx!.beginPath()
    ctx!.moveTo(ax, ay)
    ctx!.lineTo(bx, by)
    ctx!.stroke()
  })

  vis.forEach(n => {
    const [sx, sy] = worldToScreen(n.x, n.y)
    const r = nodeRadius(n) * dpr * Math.max(0.7, Math.min(1.6, camera.scale))
    const dim = highlightSet && !highlightSet.has(n.id)
    const isSel = n.id === selectedId.value || pathIds.value.has(n.id)
    ctx!.beginPath()
    ctx!.arc(sx, sy, r, 0, Math.PI * 2)
    ctx!.fillStyle = dim ? hexToRgba(colorForNode(n), 0.18) : colorForNode(n)
    ctx!.fill()
    if (isSel) {
      ctx!.lineWidth = 2 * dpr
      ctx!.strokeStyle = '#fff'
      ctx!.stroke()
    }
    if (!dim && (camera.scale > 0.55 || isSel || n.degree > 3)) {
      ctx!.fillStyle = dim ? 'rgba(231,232,240,0.25)' : 'rgba(231,232,240,0.85)'
      ctx!.font = `${11 * dpr}px -apple-system, sans-serif`
      ctx!.textBaseline = 'middle'
      ctx!.fillText(n.title, sx + r + 4, sy)
    }
  })
}

function tick() {
  stepSimulation()
  draw()
  rafId = requestAnimationFrame(tick)
}

function hitTest(wx: number, wy: number): GraphNode | null {
  const vis = visibleNodes.value
  for (let i = vis.length - 1; i >= 0; i--) {
    const n = vis[i]
    const r = nodeRadius(n) + 3
    const dx = n.x - wx
    const dy = n.y - wy
    if (dx * dx + dy * dy <= r * r) return n
  }
  return null
}

let dragging: GraphNode | null = null
let panStart: { x: number; y: number; cx: number; cy: number } | null = null
let dragged = false

function onMouseDown(e: MouseEvent) {
  if (!canvasEl.value) return
  const rect = canvasEl.value.getBoundingClientRect()
  const [wx, wy] = screenToWorld((e.clientX - rect.left) * dpr, (e.clientY - rect.top) * dpr)
  const hit = hitTest(wx, wy)
  dragged = false
  const shiftKey = e.shiftKey
  if (hit) {
    dragging = hit
    ;(dragging as any)._shiftIntent = shiftKey
  } else {
    panStart = { x: e.clientX, y: e.clientY, cx: camera.x, cy: camera.y }
  }
  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('mouseup', onMouseUp)
}
function onMouseMove(e: MouseEvent) {
  if (dragging) {
    dragged = true
    if (!canvasEl.value) return
    const rect = canvasEl.value.getBoundingClientRect()
    const [wx, wy] = screenToWorld((e.clientX - rect.left) * dpr, (e.clientY - rect.top) * dpr)
    dragging.x = wx
    dragging.y = wy
    dragging.vx = 0
    dragging.vy = 0
  } else if (panStart) {
    dragged = true
    camera.x = panStart.cx + (e.clientX - panStart.x) * dpr
    camera.y = panStart.cy + (e.clientY - panStart.y) * dpr
  }
}
function onMouseUp() {
  if (dragging && !dragged) handleNodeClick(dragging.id, !!(dragging as any)._shiftIntent)
  else if (!dragging && !dragged) selectNode(null)
  dragging = null
  panStart = null
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
}
function onWheel(e: WheelEvent) {
  const delta = -e.deltaY * 0.0012
  camera.scale = Math.max(0.15, Math.min(3, camera.scale * (1 + delta)))
}

// ---------- list view ----------
const view = ref<'graph' | 'list'>('graph')
const sortKey = ref<'title' | 'type' | 'degree'>('title')
const sortDir = ref(1)
function setSort(key: 'title' | 'type' | 'degree') {
  if (sortKey.value === key) sortDir.value *= -1
  else { sortKey.value = key; sortDir.value = 1 }
}
const sortedVisibleNodes = computed(() => {
  const arr = [...visibleNodes.value]
  arr.sort((a, b) => {
    const av = sortKey.value === 'degree' ? a.degree : (a as any)[sortKey.value] || ''
    const bv = sortKey.value === 'degree' ? b.degree : (b as any)[sortKey.value] || ''
    if (av < bv) return -1 * sortDir.value
    if (av > bv) return 1 * sortDir.value
    return 0
  })
  return arr
})

// ---------- lifecycle ----------
onMounted(async () => {
  await loadGraph()
  await nextTick()
  if (canvasEl.value) ctx = canvasEl.value.getContext('2d')
  resizeCanvas()
  if (canvasWrap.value) {
    ro = new ResizeObserver(() => resizeCanvas())
    ro.observe(canvasWrap.value)
  }
  rafId = requestAnimationFrame(tick)
})
onBeforeUnmount(() => {
  if (rafId) cancelAnimationFrame(rafId)
  ro?.disconnect()
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
})
</script>

<style scoped>
.memory-map {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.mm-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 210px 1fr 280px;
}
@media (max-width: 900px) {
  .mm-body { grid-template-columns: 1fr; }
  .mm-sidebar, .mm-detail { display: none; }
}

.mm-sidebar, .mm-detail {
  overflow-y: auto;
  padding: var(--space-3);
  background: var(--bg2);
}
.mm-sidebar { border-right: 1px solid var(--border); }
.mm-detail { border-left: 1px solid var(--border); }

.mm-sidebar h3, .mm-detail h4 {
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--fg3);
  margin: var(--space-4) 0 var(--space-2);
}
.mm-sidebar h3:first-child { margin-top: 0; }
.mm-row-between { display: flex; align-items: baseline; justify-content: space-between; }
.mm-link { background: none; border: none; color: var(--accent); font-size: var(--text-xs); cursor: pointer; padding: 0; }
.mm-hint { color: var(--fg3); font-size: var(--text-xs); margin: 0; }

.mm-stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
.mm-stat { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px 8px; }
.mm-stat .n { font-size: var(--text-lg); font-weight: 600; }
.mm-stat .l { font-size: var(--text-xs); color: var(--fg3); }

.mm-search input { width: 100%; font-size: var(--text-sm); }

.mm-chip-row { display: flex; flex-direction: column; gap: 2px; }
.mm-chip {
  display: flex; align-items: center; gap: 7px; padding: 5px 6px; border-radius: var(--radius-sm);
  cursor: pointer; font-size: var(--text-sm); color: var(--fg2);
}
.mm-chip:hover { background: var(--bg3); }
.mm-chip.off { opacity: 0.35; }
.mm-chip .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.mm-chip .cnt { margin-left: auto; color: var(--fg3); font-variant-numeric: tabular-nums; }
.mm-chip .only {
  display: none; margin-left: auto; background: none; border: none; color: var(--accent);
  font-size: var(--text-xs); padding: 1px 4px; border-radius: 4px; cursor: pointer;
}
.mm-chip:hover .cnt { display: none; }
.mm-chip:hover .only { display: inline; }

.mm-link-list { display: flex; flex-direction: column; gap: 2px; }
.mm-link-item {
  display: flex; align-items: center; gap: 6px; padding: 5px 6px; border-radius: var(--radius-sm);
  cursor: pointer; font-size: var(--text-sm); color: var(--fg);
}
.mm-link-item:hover { background: var(--bg3); }
.mm-link-item .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.mm-link-item .cnt { margin-left: auto; color: var(--fg3); }

.mm-canvas-wrap { position: relative; overflow: hidden; background: var(--bg); }
.mm-canvas-wrap canvas { display: block; width: 100%; height: 100%; cursor: grab; }
.mm-zoom-controls { position: absolute; top: var(--space-3); right: var(--space-3); display: flex; flex-direction: column; gap: 6px; }
.mm-hint-overlay {
  position: absolute; bottom: var(--space-3); left: var(--space-3); font-size: var(--text-xs); color: var(--fg3);
  background: color-mix(in srgb, var(--bg) 70%, transparent); padding: 4px 8px; border-radius: var(--radius-sm);
}

.mm-empty, .mm-empty-detail { color: var(--fg3); font-size: var(--text-sm); padding: var(--space-5); text-align: center; }

.mm-list-wrap { overflow: auto; padding: var(--space-4); }
.mm-list-wrap table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
.mm-list-wrap thead th {
  text-align: left; padding: 6px 10px; color: var(--fg3); font-weight: 500; font-size: var(--text-xs);
  text-transform: uppercase; letter-spacing: 0.04em; cursor: pointer; border-bottom: 1px solid var(--border);
}
.mm-list-wrap tbody td { padding: 6px 10px; border-bottom: 1px solid var(--bg3); }
.mm-list-wrap tbody tr:hover { background: var(--bg3); cursor: pointer; }
.mm-list-wrap .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 7px; }
.mm-list-wrap .muted { color: var(--fg3); }
.tag-mini { display: inline-block; background: var(--bg3); color: var(--fg2); border-radius: 4px; padding: 1px 6px; font-size: var(--text-xs); margin: 0 3px 2px 0; }

.mm-detail-type { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--fg3); }
.mm-detail-title { font-size: var(--text-lg); font-weight: 600; margin: 4px 0 var(--space-2); }
.mm-detail-desc { color: var(--fg2); font-size: var(--text-sm); margin-bottom: var(--space-3); }
.mm-detail-path { font-size: var(--text-xs); color: var(--fg3); word-break: break-all; }
.mm-detail-section { margin: var(--space-3) 0; }
.pill { display: inline-block; background: var(--bg3); border-radius: var(--radius-pill); padding: 2px 9px; font-size: var(--text-xs); margin: 0 4px 4px 0; color: var(--fg2); }

.mm-seg, .mm-workspace-toggle { display: flex; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }
.mm-seg button, .mm-workspace-toggle button {
  background: transparent; border: none; color: var(--fg2); padding: 6px 12px; font-size: var(--text-sm); cursor: pointer; font-family: var(--font);
}
.mm-seg button.active, .mm-workspace-toggle button.active { background: var(--accent); color: #fff; }
</style>
