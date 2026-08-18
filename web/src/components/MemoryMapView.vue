<template>
  <div class="memory-map">
    <PaneHeader page-tag="memory" @open-sidebar="emit('open-sidebar')">
      <template #actions>
        <div class="mm-seg">
          <button type="button" :class="{ active: view === 'graph' }" @click="view = 'graph'">Graph</button>
          <button type="button" :class="{ active: view === 'list' }" @click="view = 'list'">List</button>
        </div>
      </template>
    </PaneHeader>

    <div class="mm-body">
      <div v-if="mm.loading" class="mm-empty">Loading vault graph…</div>
      <div v-else-if="mm.loadError" class="mm-empty">{{ mm.loadError }}</div>
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
            <tr v-for="n in sortedVisibleNodes" :key="n.id" @click="mm.selectNode(n.id)">
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
        <div v-if="!mm.selectedNode" class="mm-empty-detail">
          Select a note to see its tags, description, and what it links to.
        </div>
        <template v-else>
          <div class="mm-detail-type">{{ categoryLabelFor(mm.selectedNode) }}</div>
          <div class="mm-detail-title">{{ mm.selectedNode.title }}</div>
          <div v-if="mm.selectedNode.description" class="mm-detail-desc">{{ mm.selectedNode.description }}</div>
          <div class="mm-detail-path">{{ mm.selectedNode.id }}</div>

          <div v-if="mm.selectedNode.tags.length" class="mm-detail-section">
            <h4>Tags</h4>
            <span v-for="t in mm.selectedNode.tags" :key="t" class="pill">{{ t }}</span>
          </div>
          <div v-if="mm.selectedNode.aliases.length" class="mm-detail-section">
            <h4>Aliases</h4>
            <span v-for="a in mm.selectedNode.aliases" :key="a" class="pill">{{ a }}</span>
          </div>

          <div class="mm-detail-section">
            <h4>Linked notes ({{ mm.neighborsOf(mm.selectedNode.id).length }})</h4>
            <div v-if="!mm.neighborsOf(mm.selectedNode.id).length" class="mm-hint">No links found — orphaned note.</div>
            <div
              v-for="nb in mm.neighborsOf(mm.selectedNode.id)"
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
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import PaneHeader from './PaneHeader.vue'
import { useProjectStore } from '../stores/projects'
import { useMemoryMapStore, categoryLabelFor, categoryColorFor, catKeyFor, type MemoryGraphNode } from '../stores/memoryMap'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const store = useProjectStore()
const mm = useMemoryMapStore()

function colorForNode(n: MemoryGraphNode): string {
  return categoryColorFor(catKeyFor(n))
}

// The graph always follows the workspace switcher shared with every other
// view (sidebar toggle, number-key shortcut, chat header) — the store
// itself watches `store.activeWorkspace` and reloads, so a switch made
// while this view isn't even mounted still lands correctly next time it is.
watch(() => store.activeWorkspace, () => resetCamera())

// ---------- selection / detail panel ----------
// The sidebar's "most connected" list and this view's own neighbor links
// both go through `mm.requestFocus`, which bumps `focusSignal` below; the
// canvas is the only thing that knows how to pan/zoom, so it is the only
// thing that reacts to it.
function focusNode(id: string) {
  mm.requestFocus(id)
}
watch(() => mm.focusSignal.seq, () => {
  const id = mm.focusSignal.id
  if (!id) return
  const n = mm.nodesById.get(id)
  if (n) {
    camera.x = -n.x * camera.scale
    camera.y = -n.y * camera.scale
  }
})

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
function nodeRadius(n: MemoryGraphNode): number {
  return 4 + Math.min(10, Math.sqrt(n.degree + 1) * 2.4)
}
function hexToRgba(hex: string, a: number): string {
  const v = hex.replace('#', '')
  const r = parseInt(v.substring(0, 2), 16)
  const g = parseInt(v.substring(2, 4), 16)
  const b = parseInt(v.substring(4, 6), 16)
  return `rgba(${r},${g},${b},${a})`
}

// `v-if="loading"` (and the load-error branch) unmount the `<canvas>` and
// its wrapper entirely while a fetch is in flight — including on a
// workspace switch, which reloads the graph. Re-acquiring the 2D context
// and re-observing the new wrapper here, instead of only once in
// onMounted, is what keeps the canvas from going permanently blank after a
// workspace switch: without it, `ctx`/`ro` kept pointing at the DOM nodes
// from the *previous* graph load, which had already been discarded.
function attachCanvas() {
  if (!canvasEl.value || !canvasWrap.value) return
  ctx = canvasEl.value.getContext('2d')
  ro?.disconnect()
  ro = new ResizeObserver(() => resizeCanvas())
  ro.observe(canvasWrap.value)
  resizeCanvas()
}
watch(canvasEl, (el) => {
  if (el) nextTick(() => attachCanvas())
})

function stepSimulation() {
  const vis = mm.visibleNodes
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
  const visSet = mm.visibleIds
  mm.edges.forEach(e => {
    if (!visSet.has(e.source) || !visSet.has(e.target)) return
    const a = mm.nodesById.get(e.source)
    const b = mm.nodesById.get(e.target)
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
  const vis = mm.visibleNodes
  const visSet = mm.visibleIds
  const highlightSet = mm.pathIds.size
    ? mm.pathIds
    : mm.selectedId
      ? new Set([mm.selectedId, ...(mm.adjacency.get(mm.selectedId) || [])])
      : null

  ctx.lineWidth = dpr
  mm.edges.forEach(e => {
    if (!visSet.has(e.source) || !visSet.has(e.target)) return
    const a = mm.nodesById.get(e.source)
    const b = mm.nodesById.get(e.target)
    if (!a || !b) return
    const onPath = mm.pathIds.has(e.source) && mm.pathIds.has(e.target)
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
    const isSel = n.id === mm.selectedId || mm.pathIds.has(n.id)
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

function hitTest(wx: number, wy: number): MemoryGraphNode | null {
  const vis = mm.visibleNodes
  for (let i = vis.length - 1; i >= 0; i--) {
    const n = vis[i]
    const r = nodeRadius(n) + 3
    const dx = n.x - wx
    const dy = n.y - wy
    if (dx * dx + dy * dy <= r * r) return n
  }
  return null
}

let dragging: MemoryGraphNode | null = null
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
  if (dragging && !dragged) mm.handleNodeClick(dragging.id, !!(dragging as any)._shiftIntent)
  else if (!dragging && !dragged) mm.selectNode(null)
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
  const arr = [...mm.visibleNodes]
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
  await mm.loadGraph(store.activeWorkspace)
  resetCamera()
  await nextTick()
  attachCanvas()
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
  grid-template-columns: 1fr 280px;
}
@media (max-width: 900px) {
  .mm-body { grid-template-columns: 1fr; }
  .mm-detail { display: none; }
}

.mm-detail {
  overflow-y: auto;
  padding: var(--space-3);
  background: var(--bg2);
  border-left: 1px solid var(--border);
}

.mm-detail h4 {
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--fg3);
  margin: var(--space-4) 0 var(--space-2);
}
.mm-hint { color: var(--fg3); font-size: var(--text-xs); margin: 0; }

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

.mm-seg { display: flex; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }
.mm-seg button {
  background: transparent; border: none; color: var(--fg2); padding: 6px 12px; font-size: var(--text-sm); cursor: pointer; font-family: var(--font);
}
.mm-seg button.active { background: var(--accent); color: #fff; }
</style>
