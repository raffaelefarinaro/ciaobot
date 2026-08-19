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

    <div class="mm-body" :class="{ 'mm-body--detail-open': !!mm.selectedNode }">
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

      <aside v-if="mm.selectedNode" class="mm-detail">
        <button
          type="button"
          class="mm-detail-close"
          title="Close (Esc)"
          aria-label="Close note detail"
          @click="mm.selectNode(null)"
        >×</button>
        <div class="mm-detail-type">{{ categoryLabelFor(mm.selectedNode) }}</div>
        <div class="mm-detail-title">{{ mm.selectedNode.title }}</div>
        <div v-if="mm.selectedNode.description" class="mm-detail-desc">{{ mm.selectedNode.description }}</div>
        <button type="button" class="mm-detail-path" @click="openNoteFile(mm.selectedNode.id)">{{ mm.selectedNode.id }}</button>

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
          >
            <span class="dot" :style="{ background: colorForNode(nb) }" />
            <span class="label mm-link-label" @click="openNoteFile(nb.id)">{{ nb.title }}</span>
            <button
              type="button"
              class="mm-link-focus"
              title="Locate in graph"
              aria-label="Locate in graph"
              @click.stop="focusNode(nb.id)"
            >⌖</button>
          </div>
        </div>

        <div class="mm-detail-section">
          <button type="button" class="mm-delete-btn" :disabled="deletingNote" @click="deleteNote(mm.selectedNode.id)">
            {{ deletingNote ? 'Deleting…' : 'Delete note' }}
          </button>
        </div>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import PaneHeader from './PaneHeader.vue'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import { useMemoryMapStore, categoryLabelFor, categoryColorFor, catKeyFor, type MemoryGraphNode } from '../stores/memoryMap'
import { askConfirm } from '../lib/confirm'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const store = useProjectStore()
const mm = useMemoryMapStore()
const fileViewer = useFileViewerStore()

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
function openNoteFile(id: string) {
  // "Navigating" to a linked note from the detail panel should feel like
  // clicking it in the graph: the map's highlight and the panel's own
  // content follow, on top of opening the file.
  mm.requestFocus(id)
  void fileViewer.open(id)
}
const deletingNote = ref(false)
async function deleteNote(id: string) {
  if (deletingNote.value) return
  const title = mm.nodesById.get(id)?.title || id
  if (!await askConfirm(`Delete "${title}"? This permanently removes the note file and cannot be undone.`, {
    title: 'Delete note', confirmLabel: 'Delete note', destructive: true,
  })) return
  deletingNote.value = true
  try {
    await mm.deleteNote(id)
  } catch (err) {
    store.pushErrorToast('Could not delete note', err instanceof Error ? err.message : 'Could not delete note')
  } finally {
    deletingNote.value = false
  }
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
// The physics step never reaches exactly zero velocity on its own, so
// without a convergence check the layout would redraw forever — a constant,
// faint jitter that reads as "the app keeps refreshing" once the graph has
// visibly settled. Stop scheduling frames once every node's speed has been
// below the threshold for SETTLE_FRAMES_REQUIRED in a row, and only wake it
// again on something that can actually move nodes.
//
// That alone isn't enough at real vault scale, though: a hub note with 60+
// links asks 60+ neighbors to all sit ~SPRING_LEN from it while also mutually
// repelling each other, which has no configuration where every pairwise force
// is simultaneously satisfied — a few nodes keep oscillating indefinitely
// instead of settling under naive damping (confirmed: a 5-note test vault
// converges fine, a 318-note one with such a hub does not). A cooling
// schedule — the same technique classic force-directed layouts
// (Fruchterman-Reingold) use — bounds convergence time regardless: force
// output is scaled by a "temperature" that ramps from 1 to 0 over a fixed
// step budget, so velocity is driven to exactly zero by the time the budget
// elapses no matter how the layout was oscillating. Velocity clamping
// (MAX_SPEED) additionally stops any single step from overshooting wildly,
// which is what let close-packed nodes near a hub swing further apart each
// step instead of settling closer.
const SETTLE_VELOCITY_EPS = 0.05
const SETTLE_FRAMES_REQUIRED = 30
const MAX_SPEED = 40
let calmFrames = 0
// The *visible* cooling window is a wall-clock budget, not a frame count: a
// frame-count budget converged in ~15s on a real 300-note vault instead of
// the intended "a few seconds", because each step's O(n^2) repulsion cost
// (and this environment's software canvas) meant far fewer frames actually
// ran per second than assumed. Tying it to real elapsed time instead makes
// "how long the settle animation visibly runs" independent of frame rate,
// node count, or how fast any given machine/browser executes each step.
const COOLING_DURATION_MS = 2500
let coolingStartedAt = 0
// The one-time synchronous warmup (before first paint, so it costs load
// time rather than animation time) still scales with node count: a denser,
// hub-heavy graph needs more iterations to work out a stable configuration
// before the user ever sees it.
function warmupStepsFor(nodeCount: number): number {
  return Math.max(80, Math.min(400, nodeCount * 3))
}
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
  // draw() only runs inside the RAF loop; without this, resetting the
  // camera while the graph is at rest changed the reactive state but the
  // canvas kept showing the old view until something else woke it up.
  requestRedraw()
}
function zoom(factor: number) {
  camera.scale = Math.max(0.15, Math.min(3, camera.scale * factor))
  requestRedraw()
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
  // Every (re)attachment is a fresh graph (initial load, or a workspace
  // switch since the canvas is torn down and rebuilt for each) — warm up
  // before the first paint, then run the brief settle animation.
  warmupSimulation(warmupStepsFor(mm.visibleNodes.length))
  wakeSimulation()
}
// A category/search filter change can bring previously-hidden nodes back
// into the simulation; wake it so they settle instead of sitting inert at
// whatever position they last had.
watch(() => mm.visibleIds, () => wakeSimulation())
// draw() only ever runs inside the RAF loop, which stops once the layout is
// calm (see tick()) — so changing which node is selected/on-path while the
// graph is at rest updated the reactive state but never repainted, and the
// canvas kept showing whichever node was highlighted last. A selection
// change doesn't need physics, just one more frame; waking the existing loop
// is simpler than adding a second, physics-free redraw path.
watch(() => [mm.selectedId, mm.pathStart, mm.pathEnd], () => wakeSimulation())
watch(canvasEl, (el) => {
  if (el) nextTick(() => attachCanvas())
})

/** Advances the layout by one step; returns the fastest node's speed this step. */
/** @param cooling 1 = full force, ramping to 0 forces velocity to zero regardless of residual imbalance. */
function stepSimulation(cooling = 1): number {
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
  let maxSpeed = 0
  vis.forEach(n => {
    const f = forces.get(n.id)
    if (!f) return
    n.vx = (n.vx + f.fx) * DAMP * cooling
    n.vy = (n.vy + f.fy) * DAMP * cooling
    const rawSpeed = Math.hypot(n.vx, n.vy)
    if (rawSpeed > MAX_SPEED) {
      const scale = MAX_SPEED / rawSpeed
      n.vx *= scale
      n.vy *= scale
    }
    n.x += n.vx
    n.y += n.vy
    const speed = Math.hypot(n.vx, n.vy)
    if (speed > maxSpeed) maxSpeed = speed
  })
  return maxSpeed
}

/** Run the layout forward without painting, so the graph starts near its
 * settled shape instead of visibly exploding outward from random starting
 * positions — noisy and hard to read with a large vault. Step count scales
 * with node count (see warmupStepsFor) so a dense, hub-heavy vault gets
 * proportionally more (still synchronous, pre-paint) time to work out a
 * stable configuration instead of ending warmup still mid-oscillation. */
function warmupSimulation(steps: number) {
  for (let i = 0; i < steps; i++) stepSimulation(Math.max(0, 1 - i / steps))
}

function wakeSimulation() {
  calmFrames = 0
  coolingStartedAt = performance.now()
  if (!rafId) rafId = requestAnimationFrame(tick)
}

// A pure camera change (pan, wheel-zoom, the zoom buttons, resetCamera) needs
// exactly one more paint, not a restarted physics run: `wakeSimulation()`
// re-arms the ~2.5s cooling schedule and repulsion stepping, which is wasted
// work for a camera move and was itself making zoom feel heavy. Once the
// layout is calm the RAF loop is stopped entirely (see tick()), so without
// this, moving the camera while idle updated `camera` but nothing redrew
// until some other interaction happened to wake the physics loop — this was
// the "stuck" pan/zoom bug. Schedules at most one extra frame; a no-op while
// the physics loop is already running, since tick() draws every frame anyway.
let redrawRafId = 0
function requestRedraw() {
  if (rafId || redrawRafId) return
  redrawRafId = requestAnimationFrame(() => {
    redrawRafId = 0
    draw()
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
  // cooling ramps 1 -> 0 over COOLING_DURATION_MS of real elapsed time; once
  // past that budget it stays at 0, which forces velocity to exactly zero
  // every subsequent step (see stepSimulation) — a hard, wall-clock bound on
  // how long this can possibly keep animating, regardless of node count,
  // frame rate, or whether the layout ever reaches a true low-energy
  // equilibrium on its own.
  const elapsed = performance.now() - coolingStartedAt
  const cooling = Math.max(0, 1 - elapsed / COOLING_DURATION_MS)
  const maxSpeed = stepSimulation(cooling)
  draw()
  if (maxSpeed < SETTLE_VELOCITY_EPS) {
    calmFrames += 1
    if (calmFrames >= SETTLE_FRAMES_REQUIRED) {
      // Layout is at rest: stop animating rather than redrawing an
      // unchanging frame forever. wakeSimulation() resumes it.
      rafId = 0
      return
    }
  } else {
    calmFrames = 0
  }
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

// A plain click has to survive a few pixels of incidental pointer jitter
// between mousedown and mouseup, or it reads as a drag every time — this was
// the "it keeps thinking I want to move it" complaint. Nothing (node move,
// pan) actually happens until the pointer clears this threshold; a release
// before that is unambiguously a click.
const CLICK_DRAG_THRESHOLD_PX = 4
let hitNode: MemoryGraphNode | null = null
let dragging: MemoryGraphNode | null = null
let panStart: { x: number; y: number; cx: number; cy: number } | null = null
let downPos: { x: number; y: number } | null = null
let dragged = false

function onMouseDown(e: MouseEvent) {
  if (!canvasEl.value) return
  const rect = canvasEl.value.getBoundingClientRect()
  const [wx, wy] = screenToWorld((e.clientX - rect.left) * dpr, (e.clientY - rect.top) * dpr)
  hitNode = hitTest(wx, wy)
  dragged = false
  downPos = { x: e.clientX, y: e.clientY }
  if (hitNode) (hitNode as any)._shiftIntent = e.shiftKey
  else panStart = { x: e.clientX, y: e.clientY, cx: camera.x, cy: camera.y }
  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('mouseup', onMouseUp)
}
function onMouseMove(e: MouseEvent) {
  if (!downPos) return
  if (!dragged) {
    const dx = e.clientX - downPos.x
    const dy = e.clientY - downPos.y
    if (Math.hypot(dx, dy) < CLICK_DRAG_THRESHOLD_PX) return
    dragged = true
    if (hitNode) { dragging = hitNode; wakeSimulation() }
  }
  if (dragging) {
    if (!canvasEl.value) return
    const rect = canvasEl.value.getBoundingClientRect()
    const [wx, wy] = screenToWorld((e.clientX - rect.left) * dpr, (e.clientY - rect.top) * dpr)
    dragging.x = wx
    dragging.y = wy
    dragging.vx = 0
    dragging.vy = 0
  } else if (panStart) {
    camera.x = panStart.cx + (e.clientX - panStart.x) * dpr
    camera.y = panStart.cy + (e.clientY - panStart.y) * dpr
    requestRedraw()
  }
}
function onMouseUp() {
  if (hitNode && !dragged) mm.handleNodeClick(hitNode.id, !!(hitNode as any)._shiftIntent)
  else if (!hitNode && !dragged) mm.selectNode(null)
  hitNode = null
  dragging = null
  panStart = null
  downPos = null
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
}
function onWheel(e: WheelEvent) {
  const delta = -e.deltaY * 0.0012
  camera.scale = Math.max(0.15, Math.min(3, camera.scale * (1 + delta)))
  requestRedraw()
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
})
onBeforeUnmount(() => {
  if (rafId) cancelAnimationFrame(rafId)
  if (redrawRafId) cancelAnimationFrame(redrawRafId)
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
  /* Same split as the chat's pinned-file panel: hidden takes no space at
     all, so the graph gets the full width until a note is selected. */
  grid-template-columns: 1fr;
}
.mm-body.mm-body--detail-open {
  grid-template-columns: 1fr 280px;
}
@media (max-width: 900px) {
  .mm-body.mm-body--detail-open { grid-template-columns: 1fr; }
  .mm-detail { display: none; }
}

.mm-detail {
  position: relative;
  overflow-y: auto;
  padding: var(--space-3);
  background: var(--bg2);
  border-left: 1px solid var(--border);
}
.mm-detail-close {
  position: absolute;
  top: var(--space-2);
  right: var(--space-2);
  background: transparent;
  border: none;
  color: var(--fg3);
  font-size: 18px;
  line-height: 1;
  width: 24px;
  height: 24px;
  cursor: pointer;
  border-radius: var(--radius-sm);
}
.mm-detail-close:hover { color: var(--fg); background: var(--bg3); }

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
  font-size: var(--text-sm); color: var(--fg);
}
.mm-link-item:hover { background: var(--bg3); }
.mm-link-item .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.mm-link-item .cnt { margin-left: auto; color: var(--fg3); }
.mm-link-label { cursor: pointer; flex: 1; }
.mm-link-label:hover { text-decoration: underline; }
.mm-link-focus {
  background: none; border: none; color: var(--fg3); cursor: pointer; padding: 2px 4px;
  border-radius: var(--radius-sm); font-size: var(--text-sm); line-height: 1; flex: none;
}
.mm-link-focus:hover { background: var(--bg2); color: var(--fg); }

.mm-canvas-wrap { position: relative; overflow: hidden; background: var(--bg); }
.mm-canvas-wrap canvas { display: block; width: 100%; height: 100%; cursor: grab; }
.mm-zoom-controls { position: absolute; top: var(--space-3); right: var(--space-3); display: flex; flex-direction: column; gap: 6px; }
.mm-hint-overlay {
  position: absolute; bottom: var(--space-3); left: var(--space-3); font-size: var(--text-xs); color: var(--fg3);
  background: color-mix(in srgb, var(--bg) 70%, transparent); padding: 4px 8px; border-radius: var(--radius-sm);
}

.mm-empty { color: var(--fg3); font-size: var(--text-sm); padding: var(--space-5); text-align: center; }

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
.mm-detail-path {
  display: block; width: 100%; text-align: left; background: none; border: none; padding: 0;
  font-family: var(--font); font-size: var(--text-xs); color: var(--fg3); word-break: break-all; cursor: pointer;
}
.mm-detail-path:hover { color: var(--fg2); text-decoration: underline; }
.mm-detail-section { margin: var(--space-3) 0; }
.mm-delete-btn {
  background: none; border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--fg3); font-family: var(--font); font-size: var(--text-sm); padding: 6px 12px; cursor: pointer;
}
.mm-delete-btn:hover:not(:disabled) { color: #f7768e; border-color: #f7768e; }
.mm-delete-btn:disabled { opacity: 0.6; cursor: default; }
.pill { display: inline-block; background: var(--bg3); border-radius: var(--radius-pill); padding: 2px 9px; font-size: var(--text-xs); margin: 0 4px 4px 0; color: var(--fg2); }

.mm-seg { display: flex; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }
.mm-seg button {
  background: transparent; border: none; color: var(--fg2); padding: 6px 12px; font-size: var(--text-sm); cursor: pointer; font-family: var(--font);
}
.mm-seg button.active { background: var(--accent); color: #fff; }
</style>
