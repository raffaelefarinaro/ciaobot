<template>
  <div class="memory-map">
    <PaneHeader page-tag="memory" @open-sidebar="emit('open-sidebar')" />

    <ProposalReviewPanel v-if="mm.view === 'review'" />

    <div v-else class="mm-body" :class="{ 'mm-body--detail-open': !!mm.selectedNode }">
      <div v-if="mm.loading" class="mm-skeleton" role="status" aria-live="polite" aria-label="Loading vault graph">
        <div class="mm-brain-skeleton" aria-hidden="true">
          <svg viewBox="0 0 200 140" class="mm-brain-svg">
            <!-- brain/network skeleton: nodes + interconnections -->
            <line x1="45" y1="38" x2="82" y2="52" class="mm-brain-edge" />
            <line x1="82" y1="52" x2="118" y2="42" class="mm-brain-edge" />
            <line x1="118" y1="42" x2="155" y2="58" class="mm-brain-edge" />
            <line x1="82" y1="52" x2="92" y2="92" class="mm-brain-edge" />
            <line x1="118" y1="42" x2="108" y2="92" class="mm-brain-edge" />
            <line x1="92" y1="92" x2="108" y2="92" class="mm-brain-edge" />
            <line x1="45" y1="38" x2="38" y2="86" class="mm-brain-edge" />
            <line x1="38" y1="86" x2="70" y2="118" class="mm-brain-edge" />
            <line x1="155" y1="58" x2="162" y2="92" class="mm-brain-edge" />
            <line x1="162" y1="92" x2="130" y2="118" class="mm-brain-edge" />
            <line x1="70" y1="118" x2="100" y2="128" class="mm-brain-edge" />
            <line x1="130" y1="118" x2="100" y2="128" class="mm-brain-edge" />
            <circle cx="45" cy="38" r="10" class="mm-brain-node mm-brain-node--1" />
            <circle cx="82" cy="52" r="7" class="mm-brain-node mm-brain-node--2" />
            <circle cx="118" cy="42" r="8" class="mm-brain-node mm-brain-node--3" />
            <circle cx="155" cy="58" r="9" class="mm-brain-node mm-brain-node--4" />
            <circle cx="92" cy="92" r="8" class="mm-brain-node mm-brain-node--2" />
            <circle cx="108" cy="92" r="7" class="mm-brain-node mm-brain-node--1" />
            <circle cx="38" cy="86" r="6" class="mm-brain-node mm-brain-node--3" />
            <circle cx="162" cy="92" r="6" class="mm-brain-node mm-brain-node--2" />
            <circle cx="70" cy="118" r="7" class="mm-brain-node mm-brain-node--4" />
            <circle cx="130" cy="118" r="7" class="mm-brain-node mm-brain-node--1" />
            <circle cx="100" cy="128" r="6" class="mm-brain-node mm-brain-node--3" />
          </svg>
        </div>
        <div class="mm-skeleton-text"><span class="history-loading-spinner" aria-hidden="true"></span> Mapping your vault…</div>
        <div class="mm-skeleton-bars" aria-hidden="true">
          <span class="mm-shimmer-line" style="width: 42%; height: 8px;"></span>
          <span class="mm-shimmer-line" style="width: 58%; height: 8px; margin-top: 8px;"></span>
        </div>
      </div>
      <div v-else-if="mm.loadError" class="mm-empty">{{ mm.loadError }}</div>
      <div v-else-if="mm.view === 'graph'" class="mm-canvas-wrap" ref="canvasWrap">
        <canvas
          ref="canvasEl"
          :class="{ 'mm-canvas--node-hover': !!hoveredNode }"
          @mousedown="onMouseDown"
          @mousemove="onCanvasHover"
          @mouseleave="clearHover"
          @wheel.prevent="onWheel"
        />
        <div class="mm-zoom-controls">
          <button type="button" class="btn-icon touch-hit" @click="zoom(1.25)">+</button>
          <button type="button" class="btn-icon touch-hit" @click="zoom(0.8)">−</button>
          <button type="button" class="btn-icon touch-hit" @click="resetCamera">⤢</button>
        </div>
        <div class="mm-toolbar">
          <div class="mm-seg mm-seg--sm" role="group" aria-label="Colour by">
            <button
              type="button"
              :class="{ active: mm.colorMode === 'category' }"
              :aria-pressed="mm.colorMode === 'category'"
              title="Colour notes by their type"
              @click="mm.setColorMode('category')"
            >Type</button>
            <button
              type="button"
              :class="{ active: mm.colorMode === 'cluster' }"
              :aria-pressed="mm.colorMode === 'cluster'"
              title="Colour notes by detected cluster"
              @click="mm.setColorMode('cluster')"
            >Clusters</button>
          </div>

          <button
            type="button"
            class="mm-toggle"
            :class="{ on: mm.hideOrphans }"
            :aria-pressed="mm.hideOrphans"
            :title="`${mm.orphanCount} notes have no links; hiding them declutters the layout`"
            @click="mm.toggleHideOrphans()"
          >{{ mm.hideOrphans ? 'Orphans hidden' : 'Hide orphans' }}</button>
        </div>

        <div class="mm-hint-overlay">
          <span>
            {{ mm.visibleNodes.length }} notes ·
            <template v-if="zoomedOut">hover a note for its name · zoom in for titles</template>
            <template v-else>click a note to light up its links · shift-click two to trace a path</template>
          </span>
        </div>
        <div
          v-if="hoveredNode"
          class="mm-hover-tip"
          :style="{ left: hoverPos.x + 'px', top: hoverPos.y + 'px' }"
        >{{ hoveredNode.title }}</div>
      </div>
      <div v-else class="mm-list-wrap">
        <table>
          <thead>
            <tr>
              <th @click="setSort('title')">Name</th>
              <th @click="setSort('type')">Type</th>
              <th>Tags</th>
              <th @click="setSort('degree')">Links</th>
              <th @click="setSort('age')" title="Days since the note's facts were last verified">Checked</th>
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
              <td :class="{ 'stale-age': n.stale }">{{ mm.ageLabelOf(n) || '—' }}<span v-if="n.stale" class="stale-flag" title="Unverified past its type's horizon">needs review</span></td>
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
        <div class="mm-detail-title">
          {{ mm.selectedNode.title }}
          <span v-if="mm.selectedNode.stale" class="stale-badge" title="Unverified past this note type's staleness horizon">needs review</span>
        </div>
        <div v-if="ageLabelOfSelected" class="mm-detail-verified">Last verified {{ ageLabelOfSelected }} ago</div>
        <div v-if="mm.selectedNode.description" class="mm-detail-desc">{{ mm.selectedNode.description }}</div>
        <button type="button" class="mm-detail-path" @click="openNoteFile(mm.selectedNode.id)">{{ mm.selectedNode.id }}</button>

        <div class="mm-detail-section mm-detail-preview">
          <h4>Content</h4>
          <div v-if="previewLoading" class="mm-preview-skeleton" aria-hidden="true">
            <span class="mm-shimmer-line" style="width: 100%; height: 8px; margin-bottom: 6px;"></span>
            <span class="mm-shimmer-line" style="width: 92%; height: 8px; margin-bottom: 6px;"></span>
            <span class="mm-shimmer-line" style="width: 88%; height: 8px; margin-bottom: 6px;"></span>
            <span class="mm-shimmer-line" style="width: 60%; height: 8px;"></span>
          </div>
          <div v-else-if="previewError" class="mm-preview-error">{{ previewError }}</div>
          <template v-else>
            <div v-if="previewContent" class="mm-preview-wrap" :class="{ 'mm-preview--collapsed': isTruncated && !expandedPreview }">
              <pre class="mm-preview-text">{{ displayedPreview }}</pre>
              <div v-if="isTruncated && !expandedPreview" class="mm-preview-fade" aria-hidden="true"></div>
            </div>
            <div v-if="!previewContent && !previewError" class="mm-hint">Empty note.</div>
            <div v-if="previewContent" class="mm-preview-actions">
              <button v-if="isTruncated" type="button" class="mm-link" @click="expandedPreview = !expandedPreview">{{ expandedPreview ? 'Show less' : 'Show more' }}</button>
              <button type="button" class="mm-link mm-link--primary" @click="openNoteFile(mm.selectedNode.id)">Open full file →</button>
            </div>
          </template>
        </div>

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
import ProposalReviewPanel from './ProposalReviewPanel.vue'
import { useProposalsStore } from '../stores/proposals'
import { router } from '../router'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import {
  useMemoryMapStore, categoryLabelFor, categoryColorFor, catKeyFor, clusterColorFor,
  type MemoryGraphNode,
} from '../stores/memoryMap'
import { askConfirm } from '../lib/confirm'
import { isLightTheme } from '../lib/theme'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const store = useProjectStore()
const mm = useMemoryMapStore()
const fileViewer = useFileViewerStore()

// The canvas paints with literal colours while the rest of the app switches
// theme through CSS custom properties, so it has to read the resolved values
// itself. (Before this, label text was hardcoded near-white and was therefore
// invisible on the light theme, whose --bg is #f4f4fa.) Cached rather than read
// per frame: getComputedStyle forces a style recalc, which is not something to
// do inside a RAF loop.
const themeColors = reactive({
  light: false,
  label: 'rgba(231,232,240,0.85)',
  edge: 'rgba(150,160,190,0.35)',
  edgeDim: 'rgba(120,126,150,0.08)',
  selectRing: '#fff',
})
function refreshThemeColors() {
  const light = isLightTheme.value
  themeColors.light = light
  themeColors.label = light ? 'rgba(32,33,48,0.88)' : 'rgba(231,232,240,0.85)'
  themeColors.edge = light ? 'rgba(80,86,120,0.35)' : 'rgba(150,160,190,0.35)'
  themeColors.edgeDim = light ? 'rgba(120,126,150,0.12)' : 'rgba(120,126,150,0.08)'
  themeColors.selectRing = light ? '#1a1a2e' : '#fff'
}
function colorForNode(n: MemoryGraphNode): string {
  if (mm.colorMode === 'cluster') return clusterColorFor(mm.clusterSlotOf(n.id), themeColors.light)
  return categoryColorFor(catKeyFor(n))
}

// Mirrors the canvas's own label threshold so the hint can tell the user why
// note titles are not on screen yet (they appear past this zoom; before it,
// names are available on hover).
const zoomedOut = computed(() => zoomRatio() < LABEL_MIN_RATIO)

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

// ---------- content preview in detail panel ----------
const previewContent = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const expandedPreview = ref(false)
const PREVIEW_LIMIT = 1200
let previewToken = 0

watch(() => mm.selectedId, async (id) => {
  expandedPreview.value = false
  if (!id) {
    previewContent.value = ''
    previewError.value = ''
    previewLoading.value = false
    return
  }
  previewLoading.value = true
  previewError.value = ''
  const token = ++previewToken
  try {
    const resp = await fetch(`/api/workspace-file?path=${encodeURIComponent(id)}`, { credentials: 'same-origin' })
    if (token !== previewToken) return
    if (!resp.ok) {
      if (resp.status === 404) previewError.value = 'File not found.'
      else if (resp.status === 413) previewError.value = 'File too large to preview.'
      else if (resp.status === 403) previewError.value = 'Cannot preview this file.'
      else previewError.value = `Failed to load (HTTP ${resp.status}).`
      previewContent.value = ''
      return
    }
    let text = await resp.text()
    // Strip YAML frontmatter for a cleaner preview — the description/tags
    // already surface frontmatter above, so showing raw `---` is noise.
    if (text.startsWith('---')) {
      const end = text.indexOf('\n---', 3)
      if (end !== -1) {
        const after = text.indexOf('\n', end + 4)
        if (after !== -1) text = text.slice(after + 1)
      }
    }
    previewContent.value = text.trimStart()
  } catch (e) {
    if (token !== previewToken) return
    previewError.value = e instanceof Error ? e.message : String(e)
    previewContent.value = ''
  } finally {
    if (token === previewToken) previewLoading.value = false
  }
})

const isTruncated = computed(() => previewContent.value.length > PREVIEW_LIMIT)
const displayedPreview = computed(() => {
  if (!isTruncated.value || expandedPreview.value) return previewContent.value
  return previewContent.value.slice(0, PREVIEW_LIMIT).trimEnd() + ' …'
})

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
// A fixed default zoom can't stay correct: the layout's extent depends on how
// many notes are visible and how hub-heavy they are, so one hardcoded number
// was simultaneously too far out for a small filtered view and badly framed
// for a full vault. Measure the settled bounding box and fit it instead.
// Falls back to the old constant only before the canvas has been sized or
// while there is nothing to frame, where there is no extent to measure.
const FIT_MARGIN = 1.12
const fitScale = ref(0.55)
/** 1 = graph exactly framed; 2 = zoomed to twice that. */
function zoomRatio(): number {
  return camera.scale / (fitScale.value || 0.55)
}
function fitCamera() {
  const vis = mm.visibleNodes
  if (!vis.length || !W || !H) {
    camera.x = 0
    camera.y = 0
    camera.scale = 0.55
    fitScale.value = 0.55
    requestRedraw()
    return
  }
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
  for (const n of vis) {
    if (n.x < minX) minX = n.x
    if (n.x > maxX) maxX = n.x
    if (n.y < minY) minY = n.y
    if (n.y > maxY) maxY = n.y
  }
  const bw = Math.max(1, maxX - minX) * FIT_MARGIN
  const bh = Math.max(1, maxY - minY) * FIT_MARGIN
  const scale = Math.max(0.15, Math.min(3, Math.min(W / bw, H / bh)))
  const cx = (minX + maxX) / 2
  const cy = (minY + maxY) / 2
  // Remember what "fully zoomed out" means for this particular graph, so the
  // label thresholds below can be expressed as "how far in has the user zoomed
  // from the framed view" instead of absolute scale values. Absolute thresholds
  // stopped being meaningful the moment the default zoom became data-dependent.
  fitScale.value = scale
  camera.scale = scale
  camera.x = -cx * scale
  camera.y = -cy * scale
  requestRedraw()
}
function resetCamera() {
  // draw() only runs inside the RAF loop; without this, resetting the
  // camera while the graph is at rest changed the reactive state but the
  // canvas kept showing the old view until something else woke it up.
  fitCamera()
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
  refreshThemeColors()
  ro?.disconnect()
  ro = new ResizeObserver(() => resizeCanvas())
  ro.observe(canvasWrap.value)
  resizeCanvas()
  // Every (re)attachment is a fresh graph (initial load, or a workspace
  // switch since the canvas is torn down and rebuilt for each) — warm up
  // before the first paint, then run the brief settle animation.
  warmupSimulation(warmupStepsFor(mm.visibleNodes.length))
  // Frame the graph only now: at onMounted the canvas had no size and the
  // nodes were still at their random start positions, so there was no extent
  // worth measuring.
  fitCamera()
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
// Colour mode changes nothing about positions, so it needs a paint, not physics.
watch(() => mm.colorMode, () => requestRedraw())
// Hiding orphans changes *which* nodes exist in the layout, so the graph has
// to be re-framed as well as re-settled — otherwise hiding them leaves the
// camera zoomed into empty space.
watch(() => mm.hideOrphans, async () => {
  await nextTick()
  warmupSimulation(warmupStepsFor(mm.visibleNodes.length))
  fitCamera()
  wakeSimulation()
})
watch(canvasEl, (el) => {
  if (el) nextTick(() => attachCanvas())
})

/** Advances the layout by one step; returns the fastest node's speed this step. */
/** @param cooling 1 = full force, ramping to 0 forces velocity to zero regardless of residual imbalance. */
// Repulsion is weighted by degree and springs are normalized by it, because a
// uniform force model turns this vault's degree distribution (top hubs at 65,
// 62, 50 links; median 2) into a hairball: 38% of notes ended up inside the
// middle 40% of the layout's radius, so the core was unreadable while the
// outer canvas sat empty. Two corrections, measured against the real 318-note
// graph:
//   - A hub's neighbours must be pushed apart from *each other*, not just from
//     the hub, so repulsion scales with both endpoints' degree.
//   - An un-normalized spring gives a 65-link note 65 inward pulls, which
//     collapses its whole neighbourhood; dividing each endpoint's pull by
//     sqrt(degree) keeps a hub from out-voting its own neighbours.
// Together these take the core fraction 38% -> 23% and raise the number of
// collision-free labels that fit at the default view by ~48%. Note that
// spreading the layout in *world* units alone does nothing for legibility —
// fitCamera() just zooms further out and the density returns; only making the
// density uniform actually buys label room.
const MIN_GAP = 40
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
    const aw = 1 + Math.sqrt(a.degree) * 0.5
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
      let f = (REPEL / d2) * aw * (1 + Math.sqrt(b.degree) * 0.5)
      // Short-range shove: 1/d^2 is too weak to separate two nodes that are
      // already nearly coincident, which is how label-on-label pairs survived
      // an otherwise settled layout.
      if (d < MIN_GAP) f += (MIN_GAP - d) * 0.25
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
    const na = Math.sqrt(Math.max(1, a.degree))
    const nb = Math.sqrt(Math.max(1, b.degree))
    if (fa) { fa.fx += fx / na; fa.fy += fy / na }
    if (fb) { fb.fx -= fx / nb; fb.fy -= fy / nb }
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
        ? themeColors.edgeDim
        : themeColors.edge
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
      ctx!.strokeStyle = themeColors.selectRing
      ctx!.stroke()
    }
  })

  // A ring under the cursor: with labels hidden at this zoom the tooltip is
  // the only name source, and the ring is what ties it to a specific dot.
  if (hoveredNode.value && visSet.has(hoveredNode.value.id)) {
    const n = hoveredNode.value
    const [sx, sy] = worldToScreen(n.x, n.y)
    const r = nodeRadius(n) * dpr * Math.max(0.7, Math.min(1.6, camera.scale))
    ctx!.beginPath()
    ctx!.arc(sx, sy, r + 3 * dpr, 0, Math.PI * 2)
    ctx!.lineWidth = 1.5 * dpr
    ctx!.strokeStyle = themeColors.selectRing
    ctx!.stroke()
  }

  drawLabels(vis, highlightSet)
}

// Labels get their own pass, run after every node is on screen, because
// deciding what to label is a competition for screen space and the old
// single-pass version never held one. Two things made it unreadable at real
// vault scale: `camera.scale > 0.55` is true for *every* node as soon as you
// zoom past the default, so all ~300 titles painted at once; and nothing
// checked whether a label landed on top of one already drawn. Here the
// candidates are ranked (selection first, then hubs), then each one is placed
// only if its box is still free — so density is capped by the pixels actually
// available rather than by a node count, and the labels that survive are the
// ones worth reading.
const LABEL_FONT_PX = 11
const LABEL_MAX_W = 170
const LABEL_PAD = 3
const LABEL_CELL = 96

// Below this zoom the canvas draws *no* labels at all: at the framed view a
// 300-note vault has no room for 300 titles, and even a handful of pills over
// the hairball read as clutter. Names stay reachable two other ways — hover
// shows one under the cursor, and zooming past this ratio brings titles back.
const LABEL_MIN_RATIO = 1.6
/**
 * The degree floor is a response to label *pressure*, not to zoom on its own.
 * A small filtered view framed at fit scale has a low zoom ratio but acres of
 * free space, and hiding every title there would hide the only four notes the
 * user came to read. At or below this many visible notes, the canvas is sparse
 * enough that labels are exempt from both the zoom gate and the degree floor.
 */
const SPARSE_VIEW_NODES = 40
// Minimum degree a node needs before it may claim a label, graded by how far
// the user has zoomed in from the framed view: far out only hubs are legible at
// all, close in everything that fits is welcome. This replaces the binary
// hubs-only/everything switch that made one notch of zoom paint all 318.
function labelDegreeFloor(visibleCount: number): number {
  // A sparse view has room for everything; gating by degree there would leave
  // a local neighbourhood of leaf notes completely unlabelled.
  if (visibleCount <= SPARSE_VIEW_NODES) return 0
  const ratio = zoomRatio()
  if (ratio >= 3.5) return 0
  if (ratio >= 2.5) return 1
  return 3
}

function ellipsize(text: string, maxW: number): string {
  if (ctx!.measureText(text).width <= maxW) return text
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (ctx!.measureText(text.slice(0, mid) + '\u2026').width <= maxW) lo = mid
    else hi = mid - 1
  }
  return text.slice(0, lo) + '\u2026'
}

function drawLabels(vis: MemoryGraphNode[], highlightSet: Set<string> | null) {
  // Far out, nothing at all: titles only earn their pixels once the user has
  // zoomed in past LABEL_MIN_RATIO — or the view is sparse enough not to need
  // the room. Identification before that is hover's job.
  if (vis.length > SPARSE_VIEW_NODES && zoomRatio() < LABEL_MIN_RATIO) return
  ctx!.font = `${LABEL_FONT_PX * dpr}px -apple-system, sans-serif`
  ctx!.textBaseline = 'middle'
  ctx!.fillStyle = themeColors.label

  const floor = labelDegreeFloor(vis.length)
  const halfH = (LABEL_FONT_PX / 2 + LABEL_PAD) * dpr
  const maxW = LABEL_MAX_W * dpr
  const cell = LABEL_CELL * dpr
  // Bucketing the placed boxes into a coarse grid keeps the overlap test near
  // constant time per label instead of comparing against every earlier one.
  const taken = new Map<string, number[][]>()
  const bounds = (x0: number, y0: number, x1: number, y1: number) => [
    Math.floor(x0 / cell), Math.floor(y0 / cell),
    Math.floor(x1 / cell), Math.floor(y1 / cell),
  ]
  function free(x0: number, y0: number, x1: number, y1: number): boolean {
    const [c0, r0, c1, r1] = bounds(x0, y0, x1, y1)
    for (let c = c0; c <= c1; c++) {
      for (let r = r0; r <= r1; r++) {
        const bucket = taken.get(`${c}:${r}`)
        if (!bucket) continue
        for (const b of bucket) {
          if (x0 < b[2] && x1 > b[0] && y0 < b[3] && y1 > b[1]) return false
        }
      }
    }
    return true
  }
  function claim(x0: number, y0: number, x1: number, y1: number) {
    const [c0, r0, c1, r1] = bounds(x0, y0, x1, y1)
    for (let c = c0; c <= c1; c++) {
      for (let r = r0; r <= r1; r++) {
        const key = `${c}:${r}`
        const bucket = taken.get(key)
        if (bucket) bucket.push([x0, y0, x1, y1])
        else taken.set(key, [[x0, y0, x1, y1]])
      }
    }
  }

  const priority = (n: MemoryGraphNode) =>
    n.id === mm.selectedId || mm.pathIds.has(n.id) ? Number.MAX_SAFE_INTEGER : n.degree
  const candidates = vis
    .filter(n => !(highlightSet && !highlightSet.has(n.id)))
    .sort((a, b) => priority(b) - priority(a))

  for (const n of candidates) {
    const isSel = n.id === mm.selectedId || mm.pathIds.has(n.id)
    if (!isSel && n.degree < floor) continue
    const [sx, sy] = worldToScreen(n.x, n.y)
    if (sx < 0 || sx > W || sy < 0 || sy > H) continue
    const r = nodeRadius(n) * dpr * Math.max(0.7, Math.min(1.6, camera.scale))
    const text = ellipsize(n.title, maxW)
    const x0 = sx + r + 4 * dpr
    const x1 = x0 + ctx!.measureText(text).width
    const y0 = sy - halfH
    const y1 = sy + halfH
    if (!free(x0, y0, x1, y1)) continue
    claim(x0, y0, x1, y1)
    ctx!.fillText(text, x0, sy)
  }
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

// ---------- hover name overlay ----------
// With labels hidden at far-out zoom (the default framing of a real vault),
// a node's name has to be one hover away or the graph is unidentifiable.
// DOM overlay rather than canvas text so it can follow the cursor without
// waking the render loop for every pixel of movement; only the enter/leave
// of a node redraws (the highlight ring).
const hoveredNode = ref<MemoryGraphNode | null>(null)
const hoverPos = ref({ x: 0, y: 0 })
function onCanvasHover(e: MouseEvent) {
  // While a press is held (node drag, pan) the surface is "grabbed", not
  // "pointing" — a tooltip chasing the cursor mid-drag reads as noise.
  if (!canvasEl.value || downPos) {
    clearHover()
    return
  }
  const rect = canvasEl.value.getBoundingClientRect()
  const [wx, wy] = screenToWorld((e.clientX - rect.left) * dpr, (e.clientY - rect.top) * dpr)
  const n = hitTest(wx, wy)
  if (n !== hoveredNode.value) {
    hoveredNode.value = n
    requestRedraw()
  }
  if (!n || !canvasWrap.value) return
  // Below-right of the cursor, native-tooltip style, clamped inside the wrap.
  const wrapRect = canvasWrap.value.getBoundingClientRect()
  const x = e.clientX - wrapRect.left
  const y = e.clientY - wrapRect.top
  hoverPos.value = {
    x: Math.max(0, Math.min(x + 14, canvasWrap.value.clientWidth - 270)),
    y: Math.max(0, Math.min(y + 14, canvasWrap.value.clientHeight - 32)),
  }
}
function clearHover() {
  if (hoveredNode.value) requestRedraw()
  hoveredNode.value = null
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
  clearHover()
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
// Review is a segment of this view rather than its own rail entry: the proposal
// queue IS the memory system's inbox, so it belongs where you go to think about
// what Ciaobot knows. `/proposals` still routes here (the housekeeping tiles link
// to it), it just selects this segment instead of a separate page. The switcher
// itself lives in the sidebar next to the workspace toggle; this component only
// mirrors the shared `mm.view` state, seeding it from the URL on mount.
const proposals = useProposalsStore()
mm.view = router.currentRoute.value.path.startsWith('/proposals') ? 'review' : 'graph'
const sortKey = ref<'title' | 'type' | 'degree' | 'age'>('title')
const sortDir = ref(1)
function setSort(key: 'title' | 'type' | 'degree' | 'age') {
  if (sortKey.value === key) sortDir.value *= -1
  else { sortKey.value = key; sortDir.value = 1 }
}
/** Age for the detail heading; empty when the note carries no date at all. */
const ageLabelOfSelected = computed(() => {
  const n = mm.selectedNode
  if (!n) return ''
  return mm.ageLabelOf(n)
})
const sortedVisibleNodes = computed(() => {
  const arr = [...mm.visibleNodes]
  arr.sort((a, b) => {
    let av: string | number
    let bv: string | number
    if (sortKey.value === 'degree') { av = a.degree; bv = b.degree }
    else if (sortKey.value === 'age') {
      // Oldest first by default: the point of sorting by age is triage.
      av = a.ageDays ?? Number.MAX_SAFE_INTEGER
      bv = b.ageDays ?? Number.MAX_SAFE_INTEGER
    } else { av = (a as any)[sortKey.value] || ''; bv = (b as any)[sortKey.value] || '' }
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
// The canvas paints resolved colours rather than CSS vars, so a theme flip has
// to be pushed into it explicitly; the shared flag does the DOM observing.
watch(isLightTheme, () => {
  refreshThemeColors()
  requestRedraw()
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
.mm-canvas-wrap canvas.mm-canvas--node-hover { cursor: pointer; }
/* Name overlay for the label-free far-out view. pointer-events:none so it can
   never sit between the cursor and the node it names. */
.mm-hover-tip {
  position: absolute;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--fg);
  font-size: var(--text-xs);
  padding: 3px 8px;
  pointer-events: none;
  z-index: 2;
}
.mm-zoom-controls { position: absolute; top: var(--space-3); right: var(--space-3); display: flex; flex-direction: column; gap: 6px; }
.mm-hint-overlay {
  position: absolute; bottom: var(--space-3); left: var(--space-3); font-size: var(--text-xs); color: var(--fg3);
  background: color-mix(in srgb, var(--bg) 70%, transparent); padding: 4px 8px; border-radius: var(--radius-sm);
}

.mm-empty { color: var(--fg3); font-size: var(--text-sm); padding: var(--space-5); text-align: center; }

.mm-skeleton {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  padding: var(--space-5);
  min-height: 320px;
  color: var(--fg3);
  text-align: center;
}
.mm-brain-skeleton {
  width: min(360px, 80%);
  display: flex;
  justify-content: center;
}
.mm-brain-svg {
  width: 100%;
  height: auto;
  display: block;
}
.mm-brain-edge {
  stroke: var(--border);
  stroke-width: 1.5;
  stroke-linecap: round;
  opacity: 0.55;
}
.mm-brain-node {
  fill: var(--bg3);
  stroke: var(--border);
  stroke-width: 1.2;
}
.mm-brain-node--1 { animation: mm-brain-pulse 1.6s ease-in-out infinite; }
.mm-brain-node--2 { animation: mm-brain-pulse 1.6s ease-in-out infinite 0.2s; }
.mm-brain-node--3 { animation: mm-brain-pulse 1.6s ease-in-out infinite 0.4s; }
.mm-brain-node--4 { animation: mm-brain-pulse 1.6s ease-in-out infinite 0.6s; }
@keyframes mm-brain-pulse {
  0%, 100% { fill: var(--bg3); opacity: 1; }
  50% { fill: var(--bg-elev, var(--bg3)); opacity: 0.7; }
}
.history-loading-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: mm-spin 0.7s linear infinite;
  flex: none;
}
@keyframes mm-spin { to { transform: rotate(360deg); } }
.mm-skeleton-text {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg2);
  font-size: var(--text-sm);
}
.mm-skeleton-bars {
  width: min(280px, 60%);
  display: flex;
  flex-direction: column;
  align-items: center;
}
.mm-skeleton-bars .mm-shimmer-line {
  display: block;
  background: var(--bg3);
  border-radius: 4px;
  animation: title-shimmer-sweep 1.4s ease-in-out infinite;
}
@keyframes title-shimmer-sweep {
  0% { opacity: 0.6; }
  50% { opacity: 1; }
  100% { opacity: 0.6; }
}
@media (prefers-reduced-motion: reduce) {
  .mm-brain-node { animation: none; }
  .mm-skeleton-bars .mm-shimmer-line { animation: none; }
}

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
.stale-age { color: var(--warning, #ff9800); white-space: nowrap; }
.stale-flag {
  display: inline-block; margin-left: 6px;
  font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.04em;
}
.tag-mini { display: inline-block; background: var(--bg3); color: var(--fg2); border-radius: 4px; padding: 1px 6px; font-size: var(--text-xs); margin: 0 3px 2px 0; }

.mm-detail-type { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--fg3); }
.mm-detail-title { font-size: var(--text-lg); font-weight: 600; margin: 4px 0 var(--space-2); }
.mm-detail-desc { color: var(--fg2); font-size: var(--text-sm); margin-bottom: var(--space-3); }
/* Age is a warning state, not a category, so it uses the app's warning token
   rather than any type hue. */
.stale-badge {
  display: inline-block; vertical-align: middle;
  background: color-mix(in srgb, var(--warning, #ff9800) 15%, transparent);
  color: var(--warning, #ff9800);
  border-radius: var(--radius-pill); padding: 1px 9px; margin-left: 6px;
  font-size: var(--text-xs); font-weight: 500; letter-spacing: normal; text-transform: none;
}
.mm-detail-verified { color: var(--fg3); font-size: var(--text-xs); margin: -2px 0 var(--space-2); }
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

.mm-detail-preview .mm-preview-skeleton {
  padding: 4px 0;
}
.mm-detail-preview .mm-shimmer-line {
  display: block;
  background: var(--bg3);
  border-radius: 4px;
  animation: title-shimmer-sweep 1.4s ease-in-out infinite;
}
.mm-preview-wrap {
  position: relative;
  max-height: 260px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}
.mm-preview-wrap.mm-preview--collapsed {
  max-height: 220px;
}
.mm-preview-text {
  margin: 0;
  padding: 10px 12px;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--fg2);
  max-height: 260px;
  overflow: auto;
}
.mm-preview--collapsed .mm-preview-text {
  max-height: 180px;
  overflow: hidden;
}
.mm-preview-fade {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 48px;
  background: linear-gradient(to bottom, transparent, var(--bg));
  pointer-events: none;
}
.mm-preview-error {
  color: var(--warning, #ff9800);
  font-size: var(--text-xs);
  padding: 6px 0;
}
.mm-preview-actions {
  display: flex;
  gap: var(--space-2);
  margin-top: 8px;
  flex-wrap: wrap;
}
.mm-detail-preview .mm-link {
  background: none;
  border: none;
  padding: 0;
  font-family: var(--font);
  font-size: var(--text-xs);
  color: var(--accent);
  cursor: pointer;
}
.mm-detail-preview .mm-link:hover { text-decoration: underline; }
.mm-detail-preview .mm-link--primary {
  margin-left: auto;
  color: var(--fg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 3px 8px;
}
.mm-detail-preview .mm-link--primary:hover {
  color: var(--fg);
  background: var(--bg3);
  text-decoration: none;
}

.mm-seg { display: flex; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }
.mm-seg button {
  background: transparent; border: none; color: var(--fg2); padding: 6px 12px; font-size: var(--text-sm); cursor: pointer; font-family: var(--font);
}
.mm-seg button.active { background: var(--accent); color: #fff; }

/* Canvas toolbar: overlays the graph top-left, opposite the zoom controls.
   Wraps rather than scrolls so a narrow window stacks the groups instead of
   hiding the orphan toggle off the edge. */
.mm-toolbar {
  position: absolute; top: var(--space-3); left: var(--space-3);
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  max-width: calc(100% - 96px);
}
.mm-seg--sm button { padding: 4px 10px; font-size: var(--text-xs); }
.mm-toggle {
  background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--fg2); font-family: var(--font); font-size: var(--text-xs); padding: 4px 10px; cursor: pointer;
}
.mm-toggle.on { background: var(--accent); border-color: var(--accent); color: #fff; }
</style>
