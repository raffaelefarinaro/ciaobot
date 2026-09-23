<template>
  <div class="memory-map">
    <PaneHeader page-tag="memory" @open-sidebar="emit('open-sidebar')" />

    <div v-if="mm.view === 'review'" class="mm-review-wrap">
      <!-- One navigation level for everything Review holds. It used to be two:
           Proposals/Retirements here, then Queue/History inside the proposal
           panel and To review/Trash inside the retirement one — so the trash
           was a tab inside a tab inside a sidebar button, and no single row
           said what there was to decide. The four destinations are peers: two
           things waiting for a decision, two records of decisions already
           made. The underlying state is unchanged, so every existing entry
           point (the sidebar's "Needs review" list, a stale note's detail
           panel, /proposals) still lands where it did. -->
      <TabBar
        v-model="reviewTab"
        :tabs="reviewTabs"
        label="Review"
        id-prefix="mm-review"
        class="mm-review-tabs"
      />
      <!-- TabBar points every tab at `<id-prefix>-panel-<key>` via
           aria-controls, so those panels have to exist. Both components are
           single-root, so these attributes fall through onto their root
           element and change no layout.

           The proposal panel is bound without `key`, so moving between
           Suggested memories and History swaps its section rather than
           remounting it — which would refetch the queue on every flip. -->
      <ProposalReviewPanel
        v-if="reviewTab === 'proposals' || reviewTab === 'history'"
        :section="reviewTab === 'history' ? 'history' : 'queue'"
        :id="`mm-review-panel-${reviewTab}`"
        role="tabpanel"
        :aria-labelledby="`mm-review-tab-${reviewTab}`"
      />
      <!-- One component, two sections: the queue and the trash share the
           store, the busy set and the error toast, so splitting them into
           two components would only duplicate all three. -->
      <VaultReviewPanel
        v-else
        :section="reviewTab === 'trash' ? 'trash' : 'candidates'"
        :id="`mm-review-panel-${reviewTab}`"
        role="tabpanel"
        :aria-labelledby="`mm-review-tab-${reviewTab}`"
      />
    </div>

    <div v-else class="mm-body" :class="{ 'mm-body--detail-open': !!mm.selectedNode, 'mm-body--dragging-detail': isDraggingDetail }" :style="detailBodyStyle">
      <div class="mm-surface">
        <!-- Graph and List are two drawings of one set of notes, so the choice
             between them sits with the other "how should this look" controls
             rather than beside Review in the sidebar, where a rendering of the
             map read as a third page. That also makes this a real toolbar row
             shared by both views: floating over the canvas it had nowhere to
             be in the list, which is why the list had no controls at all. -->
        <div v-if="!mm.loading && !mm.loadError" class="mm-toolbar">
          <div class="mm-seg mm-seg--sm" role="group" aria-label="View">
            <button
              type="button"
              :class="{ active: mm.view === 'graph' }"
              :aria-pressed="mm.view === 'graph'"
              title="Show the vault as a graph"
              @click="setMapView('graph')"
            >Graph</button>
            <button
              type="button"
              :class="{ active: mm.view === 'list' }"
              :aria-pressed="mm.view === 'list'"
              title="Show the vault as a sortable list"
              @click="setMapView('list')"
            >List</button>
          </div>

          <button
            type="button"
            class="mm-toggle"
            :class="{ on: mm.orphanFilter === 'hide' }"
            :aria-pressed="mm.orphanFilter === 'hide'"
            :title="`${mm.orphanCount} notes have no links; hiding them declutters the layout`"
            @click="mm.toggleHideOrphans()"
          >{{ mm.orphanFilter === 'hide' ? 'Orphans hidden' : 'Hide orphans' }}</button>
          <button
            type="button"
            class="mm-toggle"
            :class="{ on: mm.orphanFilter === 'only' }"
            :aria-pressed="mm.orphanFilter === 'only'"
            :title="mm.orphanFilter === 'only' ? 'Showing only unlinked notes' : 'Show only unlinked notes — useful when you want to link them up'"
            @click="mm.toggleOnlyOrphans()"
          >{{ mm.orphanFilter === 'only' ? 'Only orphans ✓' : 'Only orphans' }}</button>
        </div>
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
              <circle cx="45" cy="38" r="13" class="mm-brain-node mm-brain-node--1" />
              <circle cx="82" cy="52" r="10" class="mm-brain-node mm-brain-node--2" />
              <circle cx="118" cy="42" r="11" class="mm-brain-node mm-brain-node--3" />
              <circle cx="155" cy="58" r="12" class="mm-brain-node mm-brain-node--4" />
              <circle cx="92" cy="92" r="11" class="mm-brain-node mm-brain-node--2" />
              <circle cx="108" cy="92" r="10" class="mm-brain-node mm-brain-node--1" />
              <circle cx="38" cy="86" r="9" class="mm-brain-node mm-brain-node--3" />
              <circle cx="162" cy="92" r="9" class="mm-brain-node mm-brain-node--2" />
              <circle cx="70" cy="118" r="10" class="mm-brain-node mm-brain-node--4" />
              <circle cx="130" cy="118" r="10" class="mm-brain-node mm-brain-node--1" />
              <circle cx="100" cy="128" r="9" class="mm-brain-node mm-brain-node--3" />
            </svg>
          </div>
          <div class="mm-skeleton-text"><span class="history-loading-spinner" aria-hidden="true"></span> Mapping your vault…</div>
          <div class="mm-skeleton-bars" aria-hidden="true">
            <span class="mm-shimmer-line" style="width: 42%; height: 8px;"></span>
            <span class="mm-shimmer-line" style="width: 58%; height: 8px; margin-top: 8px;"></span>
          </div>
        </div>
        <div v-else-if="mm.loadError" class="mm-empty">{{ mm.loadError }}</div>
        <div v-else-if="mm.view === 'graph'" class="mm-canvas-wrap" ref="canvasWrap" tabindex="0" role="region" aria-label="Vault graph">
          <canvas
            ref="canvasEl"
            :class="{ 'mm-canvas--node-hover': !!hoveredNode }"
            aria-hidden="true"
            @pointerdown="onPointerDown"
            @pointermove="onCanvasPointerMove"
            @pointerleave="clearHover"
            @pointerup="onPointerUp"
            @pointercancel="onPointerCancel"
            @lostpointercapture="onLostPointerCapture"
            @wheel.prevent="onWheel"
            @contextmenu.prevent
          />
          <div class="mm-zoom-controls">
            <button type="button" class="btn-icon touch-hit" title="Zoom in" aria-label="Zoom in" @click="zoom(1.25)">+</button>
            <button type="button" class="btn-icon touch-hit" title="Zoom out" aria-label="Zoom out" @click="zoom(0.8)">−</button>
            <button type="button" class="btn-icon touch-hit" title="Fit the whole graph" aria-label="Fit the whole graph" @click="resetCamera(true)">⤢</button>
          </div>
          <!-- Path endpoints for the current focus, mirrored from the list/detail
               controls so a keyboard user can also set them without leaving the
               canvas. Shown only while a focus exists. -->
          <div v-if="mm.selectedNode" class="mm-path-controls" role="group" aria-label="Path finder for the focused note">
            <span class="mm-path-controls-label">{{ mm.selectedNode.title }}</span>
            <button
              v-for="s in PATH_SLOTS"
              :key="s.slot"
              type="button"
              :class="['btn-chip', { active: pathSlotHolds(s.slot, mm.selectedNode.id) }]"
              :aria-pressed="pathSlotHolds(s.slot, mm.selectedNode.id)"
              @click="mm.choosePathEndpoint(mm.selectedNode.id, s.slot)"
            >{{ s.chip }}</button>
          </div>
          <div class="mm-hint-overlay">
            <span>
              {{ mm.visibleNodes.length }} notes ·
              <template v-if="zoomedOut">tap or hover a note to name it · zoom in for titles · use the List view to work by keyboard</template>
              <template v-else>tap or click to pin the neighbourhood · drag to pan · set path start/end from a note's actions</template>
            </span>
          </div>
          <div
            v-if="hoveredNode"
            class="mm-hover-tip"
            :style="{ left: hoverPos.x + 'px', top: hoverPos.y + 'px' }"
          >{{ hoveredNode.title }}</div>
        </div>
        <div v-else class="mm-list-wrap" tabindex="0" role="region" aria-label="Vault notes list">
          <table>
            <thead>
              <!-- Sortable headers state which way they are sorted, in both the
                   caret and aria-sort. They were clickable with no indicator at
                   all, so a second click on the same column looked like nothing
                   had happened. -->
              <tr>
                <th :aria-sort="ariaSort('title')">
                  <button type="button" class="mm-sort" @click="setSort('title')">
                    Name<span class="mm-sort-caret" aria-hidden="true">{{ sortCaret('title') }}</span>
                  </button>
                </th>
                <th :aria-sort="ariaSort('type')">
                  <button type="button" class="mm-sort" @click="setSort('type')">
                    Type<span class="mm-sort-caret" aria-hidden="true">{{ sortCaret('type') }}</span>
                  </button>
                </th>
                <th class="th-plain">Tags</th>
                <th :aria-sort="ariaSort('degree')">
                  <button type="button" class="mm-sort" @click="setSort('degree')">
                    Links<span class="mm-sort-caret" aria-hidden="true">{{ sortCaret('degree') }}</span>
                  </button>
                </th>
                <th :aria-sort="ariaSort('age')">
                  <button type="button" class="mm-sort" title="Days since the note's facts were last verified" @click="setSort('age')">
                    Checked<span class="mm-sort-caret" aria-hidden="true">{{ sortCaret('age') }}</span>
                  </button>
                </th>
                <th class="th-plain">Path</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="n in sortedVisibleNodes"
                :key="n.id"
                :class="{ current: mm.selectedId === n.id }"
                @click="activateRow(n, $event)"
              >
                <!-- The row opens the note by click, but a plain row handler is
                     unreachable by keyboard. A native button in the title cell
                     gives the same action a focusable, named control while the
                     row click keeps the whole-row pointer target. -->
                <td class="cell-title">
                  <button
                    type="button"
                    class="mm-title-btn"
                    :data-mm-return="n.id"
                    :aria-pressed="mm.selectedId === n.id"
                    :aria-label="`Open ${n.title}`"
                    @click.stop="activateRow(n, $event)"
                  ><span class="dot" :style="{ background: colorForNode(n) }" />{{ n.title }}</button>
                </td>
                <td class="muted">{{ categoryLabelFor(n) }}</td>
                <td>
                  <span v-for="t in n.tags.slice(0, 4)" :key="t" class="tag-mini">{{ t }}</span>
                </td>
                <!-- Link count as a bar as well as a number: sorted by links,
                     the shape of the distribution (a few hubs, a long tail of
                     twos) is the useful reading, and a column of digits hides
                     it. -->
                <td class="deg-cell">
                  <span class="deg-bar" aria-hidden="true"><span :style="{ width: degreeBarPct(n) + '%' }"></span></span>
                  <span class="deg-n">{{ n.degree }}</span>
                </td>
                <td :class="{ 'stale-age': n.stale }">{{ mm.ageLabelOf(n) || '—' }}<span v-if="n.stale" class="stale-flag" title="Unverified past its type's horizon">needs review</span></td>
                <!-- Path endpoints, usable without the graph or a pointer: the
                     list is the complete alternative to the canvas, so choosing
                     a path may not depend on shift-clicking a dot. Names carry
                     the note as well as the slot, since every row's control
                     shares a visible label. -->
                <td class="path-cell">
                  <button
                    v-for="s in PATH_SLOTS"
                    :key="s.slot"
                    type="button"
                    class="mm-path-btn"
                    :class="{ active: pathSlotHolds(s.slot, n.id) }"
                    :aria-pressed="pathSlotHolds(s.slot, n.id)"
                    :aria-label="pathSlotLabel(s.slot, n.title)"
                    @click.stop="mm.choosePathEndpoint(n.id, s.slot)"
                  >{{ s.compact }}</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

      </div>
      <aside v-if="mm.selectedNode" class="mm-detail">
        <div
          class="mm-detail-resizer"
          @mousedown="startDetailDrag"
          title="Drag to resize"
          aria-hidden="true"
        ></div>
        <button
          type="button"
          class="mm-detail-close"
          title="Close (Esc)"
          aria-label="Close note detail"
          @click="closeDetail"
        >×</button>
        <div class="mm-detail-type">{{ categoryLabelFor(mm.selectedNode) }}</div>
        <div class="mm-detail-title">
          {{ mm.selectedNode.title }}
          <span v-if="mm.selectedNode.stale" class="stale-badge" title="Unverified past this note type's staleness horizon">needs review</span>
        </div>
        <div v-if="ageLabelOfSelected" class="mm-detail-verified">Last verified {{ ageLabelOfSelected }} ago</div>
        <!-- Explicit, named path controls: the shift-click instructions on the
             canvas describe a gesture a touch or keyboard user cannot perform,
             and a note's path endpoints have to be settable from whichever
             surface actually opened it. -->
        <div class="mm-detail-path-controls" role="group" aria-label="Path finder for this note">
          <button
            v-for="s in PATH_SLOTS"
            :key="s.slot"
            type="button"
            class="btn-chip"
            :class="{ active: pathSlotHolds(s.slot, mm.selectedNode.id) }"
            :aria-pressed="pathSlotHolds(s.slot, mm.selectedNode.id)"
            @click="mm.choosePathEndpoint(mm.selectedNode.id, s.slot)"
          >{{ s.chip }}</button>
          <button
            v-if="mm.pathStart || mm.pathEnd"
            type="button"
            class="btn-chip"
            @click="mm.resetPath()"
          >Clear path</button>
        </div>
        <p v-if="mm.pathStart || mm.pathEnd" class="mm-detail-path-hint" role="status">{{ mm.pathHint }}</p>
        <button
          v-if="mm.selectedNode.stale"
          type="button"
          class="mm-detail-review-link"
          @click="openRetirementReview"
        >Review for retirement →</button>
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
            <!-- A native button, not a clickable span: opening a neighbor is a
                 core part of inspecting a note and has to be reachable by
                 keyboard and Enter/Space. -->
            <button
              type="button"
              class="label mm-link-label mm-link-btn"
              :data-mm-return="nb.id"
              :aria-label="`Open ${nb.title}`"
              @click="openNeighbor(nb.id, $event)"
            >{{ nb.title }}</button>
            <!-- Actions are grouped and non-shrinking so, at the panel's 220px
                 minimum, they wrap to a second line instead of being pushed out
                 of view by the note title. -->
            <span class="mm-link-actions">
              <button
                v-for="s in PATH_SLOTS"
                :key="s.slot"
                type="button"
                class="mm-path-btn"
                :class="{ active: pathSlotHolds(s.slot, nb.id) }"
                :aria-pressed="pathSlotHolds(s.slot, nb.id)"
                :aria-label="pathSlotLabel(s.slot, nb.title)"
                @click.stop="mm.choosePathEndpoint(nb.id, s.slot)"
              >{{ s.compact }}</button>
              <button
                type="button"
                class="mm-link-focus"
                title="Locate in graph"
                aria-label="Locate in graph"
                @click.stop="focusNode(nb.id)"
              >⌖</button>
            </span>
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
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, toRaw, watch } from 'vue'
import PaneHeader from './PaneHeader.vue'
import ProposalReviewPanel from './ProposalReviewPanel.vue'
import VaultReviewPanel from './VaultReviewPanel.vue'
import { useProposalsStore } from '../stores/proposals'
import { useVaultReviewStore } from '../stores/vaultReview'
import { router } from '../router'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import {
  useMemoryMapStore, categoryLabelFor, categoryColorFor, catKeyFor,
  type MemoryGraphNode,
} from '../stores/memoryMap'
import { askConfirm } from '../lib/confirm'
import { isLightTheme } from '../lib/theme'
import { parseFrontmatter } from '../lib/markdownFrontmatter'
import TabBar, { type TabSpec } from './TabBar.vue'
import { easeOutCubic, prefersReducedMotion, tweenCamera, type CameraState } from '../lib/cameraTween'
import { GraphGesture } from '../lib/graphGesture'
import {
  COOLING_DURATION_MS,
  DEFAULT_SCALE,
  LABEL_CELL,
  LABEL_FONT_PX,
  LABEL_MAX_W,
  LABEL_PAD,
  SETTLE_FRAMES_REQUIRED,
  SETTLE_VELOCITY_EPS,
  clampScale,
  ellipsize as ellipsizeTo,
  fitCameraFor,
  hexToRgba,
  hitTest as hitTestNodes,
  labelDegreeFloor,
  labelsVisible,
  minHitRadiusForScale,
  nodeRadius,
  screenToWorld as toWorld,
  stepSimulation as stepLayout,
  warmupStepsFor,
  worldToScreen as toScreen,
} from '../lib/graphLayout'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const store = useProjectStore()
const mm = useMemoryMapStore()
const fileViewer = useFileViewerStore()

// ---------- detail panel resizer ----------
// Mirrors ChatLayout's sidebar resizer: draggable, persisted, snaps to default.
const DETAIL_DEFAULT_WIDTH = 320
const DETAIL_MIN_WIDTH = 220
const DETAIL_MAX_WIDTH = 560
const DETAIL_SNAP_THRESHOLD = 12
function safeGetDetailWidth(): number {
  try {
    const raw = typeof localStorage !== 'undefined' ? localStorage.getItem('ciao:mm-detail-width') : null
    const n = raw ? Number(raw) : NaN
    return Number.isFinite(n) ? n : DETAIL_DEFAULT_WIDTH
  } catch {
    return DETAIL_DEFAULT_WIDTH
  }
}
function safeSetDetailWidth(v: number) {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem('ciao:mm-detail-width', String(v))
  } catch {}
}
const detailWidth = ref(safeGetDetailWidth())
const isDraggingDetail = ref(false)
// The inline grid-template-columns would otherwise outrank the
// `@media (max-width: 900px)` rule that restores a single full-width column
// (and hides the panel). Only apply the persisted width above the desktop
// breakpoint so a phone never reserves a 220-560px column for a hidden panel.
const isNarrow = ref(window.innerWidth <= 900)
function onResizeNarrow() { isNarrow.value = window.innerWidth <= 900 }
if (typeof window !== 'undefined') window.addEventListener('resize', onResizeNarrow)
const detailBodyStyle = computed(() => {
  if (!mm.selectedNode || isNarrow.value) return undefined
  return { gridTemplateColumns: `1fr ${detailWidth.value}px` } as Record<string, string>
})
let detailDragStartX = 0
let detailDragStartWidth = 0
function startDetailDrag(e: MouseEvent) {
  e.preventDefault()
  isDraggingDetail.value = true
  detailDragStartX = e.clientX
  detailDragStartWidth = detailWidth.value
  window.addEventListener('mousemove', handleDetailDrag)
  window.addEventListener('mouseup', stopDetailDrag)
  document.body.classList.add('is-dragging-layout')
}
function handleDetailDrag(e: MouseEvent) {
  if (!isDraggingDetail.value) return
  // Dragging the left edge: moving left (negative delta) grows the panel.
  const delta = detailDragStartX - e.clientX
  let next = detailDragStartWidth + delta
  if (Math.abs(next - DETAIL_DEFAULT_WIDTH) < DETAIL_SNAP_THRESHOLD) next = DETAIL_DEFAULT_WIDTH
  next = Math.max(DETAIL_MIN_WIDTH, Math.min(DETAIL_MAX_WIDTH, next))
  detailWidth.value = next
  // Keep the canvas backing store in sync while dragging, not only on release —
  // otherwise the canvas stays stretched at the old W/H until mouseup.
  resizeCanvas()
  requestRedraw()
}
function stopDetailDrag() {
  if (!isDraggingDetail.value) return
  isDraggingDetail.value = false
  safeSetDetailWidth(detailWidth.value)
  window.removeEventListener('mousemove', handleDetailDrag)
  window.removeEventListener('mouseup', stopDetailDrag)
  document.body.classList.remove('is-dragging-layout')
  // Resizing the panel changes the canvasWrap width; re-measure the canvas and
  // re-draw at the new size so it doesn't stay stretched or clipped.
  resizeCanvas()
  requestRedraw()
}

// The canvas paints with literal colours while the rest of the app switches
// theme through CSS custom properties, so it has to read the resolved values
// itself. (Before this, label text was hardcoded near-white and was therefore
// invisible on the light theme, whose --bg is #f4f4fa.) Cached rather than read
// per frame: getComputedStyle forces a style recalc, which is not something to
// do inside a RAF loop.
const themeColors = reactive({
  label: 'rgba(231,232,240,0.85)',
  edge: 'rgba(150,160,190,0.35)',
  edgeDim: 'rgba(120,126,150,0.14)',
  edgeHot: 'rgba(215,222,245,0.85)',
  selectRing: '#fff',
  staleRing: 'rgba(255,152,0,0.85)',
})
function refreshThemeColors() {
  const light = isLightTheme.value
  themeColors.label = light ? 'rgba(32,33,48,0.88)' : 'rgba(231,232,240,0.85)'
  themeColors.edge = light ? 'rgba(80,86,120,0.35)' : 'rgba(150,160,190,0.35)'
  themeColors.edgeDim = light ? 'rgba(120,126,150,0.18)' : 'rgba(120,126,150,0.14)'
  // The hover-preview edge has to read as brighter than a normal edge in dark
  // and *darker* in light; on light theme a whiter line would disappear.
  themeColors.edgeHot = light ? 'rgba(40,44,70,0.75)' : 'rgba(215,222,245,0.85)'
  themeColors.selectRing = light ? '#1a1a2e' : '#fff'
  // Amber on white needs to go darker to stay visible, same as the CSS
  // --warning token does between themes.
  themeColors.staleRing = light ? 'rgba(196,110,0,0.9)' : 'rgba(255,152,0,0.85)'
}
function colorForNode(n: MemoryGraphNode): string {
  return categoryColorFor(catKeyFor(n))
}

// The canvas's own label gate, so the hint can tell the user why note titles
// are not on screen yet (they appear past this zoom; before it, names are
// available on hover). Asking `labelsVisible` rather than re-deriving the
// threshold: the zoom gate exempts a sparse view, so a filtered view of six
// notes paints its titles at fit scale while this still claimed "zoom in for
// titles".
const zoomedOut = computed(() => !labelsVisible(mm.visibleNodes.length, zoomRatio()))

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
/** Open a linked note, remembering the neighbor control so closing the detail
 * panel returns focus to it — unless that link unmounts when the panel content
 * is replaced (see `detailReturnFocus`), in which case the watcher falls back. */
function openNeighbor(id: string, event: Event) {
  detailReturnFocus.value = { id, el: (event.currentTarget as HTMLElement | null) }
  openNoteFile(id)
}

// ---------- path endpoints ----------
// Four surfaces offer the same pair of buttons: the canvas overlay, each list
// row, the detail panel and every neighbor row. Written out by hand that was
// eight near-identical blocks, each carrying its own copy of the active class,
// the `aria-pressed` expression and (for the compact pair) the `aria-label`
// sentence — so a wording or ARIA change had eight places to land and could
// silently reach only some of them. Each surface now renders one `v-for` over
// this table, and the three bindings are computed here, once.
const PATH_SLOTS = [
  { slot: 'start' as const, chip: 'Start path', compact: 'start' },
  { slot: 'end' as const, chip: 'End path', compact: 'end' },
]
/** Whether `id` currently occupies `slot` — the active class and aria-pressed. */
function pathSlotHolds(slot: 'start' | 'end', id: string): boolean {
  return (slot === 'start' ? mm.pathStart : mm.pathEnd) === id
}
/** The accessible name for the compact buttons, whose visible text ("start")
 * is shared by every row and so names neither the note nor the action. */
function pathSlotLabel(slot: 'start' | 'end', title: string): string {
  return `Set ${title} as path ${slot}`
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
    // Strip YAML frontmatter for a cleaner preview — the description/tags
    // already surface frontmatter above, so showing raw `---` is noise. The
    // shared splitter also handles a BOM, CRLF, and a missing closing fence.
    previewContent.value = parseFrontmatter(await resp.text()).body.trimStart()
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

// How far in a magnified focus zooms, as a multiple of the framed view. Past
// LABEL_MIN_RATIO by a margin so the note that was just focused arrives with
// its title painted rather than as an anonymous dot in the middle.
const FOCUS_ZOOM_RATIO = 2.6
watch(() => mm.focusSignal.seq, () => {
  const id = mm.focusSignal.id
  if (!id) return
  // Falls back to the full node map: the sidebar's unlinked list can focus a
  // note that the current filter keeps out of the visible mirror.
  const n = simById.get(id) || mm.nodesById.get(id)
  if (!n) return
  const magnify = mm.focusSignal.magnify && zoomRatio() < FOCUS_ZOOM_RATIO
  const scale = magnify
    ? clampScale((fitScale.value || DEFAULT_SCALE) * FOCUS_ZOOM_RATIO)
    : camera.scale
  flyTo({ x: -n.x * scale, y: -n.y * scale, scale })
  // The pulse is what answers "which one is it?" on arrival. A centred dot in
  // a field of dots does not read as the destination on its own.
  pulseNode(id)
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
// Progress through the cooling schedule. The schedule itself (and why it
// exists) is in graphLayout; how far this canvas has got through it is state.
let calmFrames = 0
let coolingStartedAt = 0
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
const fitScale = ref(DEFAULT_SCALE)
/** 1 = graph exactly framed; 2 = zoomed to twice that. */
function zoomRatio(): number {
  return camera.scale / (fitScale.value || DEFAULT_SCALE)
}
/** @param animate Ease there instead of cutting — for the reset control, where
 * the user is asking to go back to a view they have seen and the tween is what
 * shows them how the current view relates to it. Layout-driven fits (first
 * paint, a filter change) still cut, since there is no continuity to preserve. */
function fitCamera(animate = false) {
  const target = fitCameraFor(simNodes, W, H)
  // Remember what "fully zoomed out" means for this particular graph, so the
  // label thresholds can be expressed as "how far in has the user zoomed from
  // the framed view" instead of absolute scale values. Absolute thresholds
  // stopped being meaningful the moment the default zoom became data-dependent.
  fitScale.value = target.scale
  if (animate && simNodes.length && W && H) {
    flyTo(target)
    return
  }
  cancelCameraTween()
  applyCamera(target)
  requestRedraw()
}
function resetCamera(animate = false) {
  // draw() only runs inside the RAF loop; without this, resetting the
  // camera while the graph is at rest changed the reactive state but the
  // canvas kept showing the old view until something else woke it up.
  fitCamera(animate)
}
function zoom(factor: number) {
  flyTo({ x: camera.x, y: camera.y, scale: clampScale(camera.scale * factor) }, ZOOM_TWEEN_MS)
}

// ---------- camera tween + arrival pulse ----------
// Both are driven by one RAF loop, separate from the physics loop: a camera
// move needs frames but must not nudge the layout, and the physics loop stops
// itself the moment the graph is calm. `animRafId` runs only while something
// is actually animating and then stops, so an idle graph still costs nothing.
const FLY_TWEEN_MS = 420
const ZOOM_TWEEN_MS = 160
const PULSE_MS = 700
let camTween: { from: CameraState; to: CameraState; startedAt: number; durMs: number } | null = null
let pulse: { id: string; startedAt: number } | null = null
let animRafId = 0

function applyCamera(state: CameraState) {
  camera.x = state.x
  camera.y = state.y
  camera.scale = state.scale
}

/** Ease the camera to `to`. Snaps instead when the OS asks for reduced motion. */
function flyTo(to: CameraState, durMs = FLY_TWEEN_MS) {
  if (prefersReducedMotion() || durMs <= 0) {
    camTween = null
    applyCamera(to)
    requestRedraw()
    return
  }
  camTween = { from: { x: camera.x, y: camera.y, scale: camera.scale }, to, startedAt: performance.now(), durMs }
  startAnimLoop()
}

/** A ring that expands and fades once around a node, marking where we landed. */
function pulseNode(id: string) {
  if (prefersReducedMotion()) return
  pulse = { id, startedAt: performance.now() }
  startAnimLoop()
}

// Any hands-on camera control wins over an in-flight tween — otherwise a fly-to
// kept dragging the view out from under a pan or a wheel gesture.
function cancelCameraTween() {
  camTween = null
}

function startAnimLoop() {
  if (animRafId) return
  animRafId = requestAnimationFrame(stepAnim)
}

function stepAnim() {
  animRafId = 0
  if (camTween) {
    const t = (performance.now() - camTween.startedAt) / camTween.durMs
    applyCamera(tweenCamera(camTween.from, camTween.to, easeOutCubic(t)))
    if (t >= 1) camTween = null
  }
  // The physics loop already draws every frame while it runs; a second draw in
  // the same frame would be pure waste.
  if (!rafId) draw()
  // draw() clears a finished pulse, so this check has to come after it.
  if (camTween || pulse) startAnimLoop()
}
// Thin wrappers so the call sites stay readable: the maths is in graphLayout,
// the camera and viewport it reads are this component's reactive state.
function worldToScreen(x: number, y: number): [number, number] {
  return toScreen(x, y, camera, W, H)
}
function screenToWorld(sx: number, sy: number): [number, number] {
  return toWorld(sx, sy, camera, W, H)
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
  // switch since the canvas is torn down and rebuilt for each).
  refreshSimNodes()
  // Frame the graph only now: at onMounted the canvas had no size and the
  // nodes were still at their random start positions, so there was no extent
  // worth measuring. The warmup re-frames as the layout expands.
  fitCamera()
  // A graph restored from the store already carries settled positions from the
  // last visit, so it only needs the short settle, not the full warmup.
  if (mm.graphIsWarm) {
    wakeSimulation()
    // Settled positions, so a focus handed over from another view can land
    // now; a cold graph waits for the warmup to place its nodes first.
    deliverPendingFocus()
  } else {
    beginWarmup(warmupStepsFor(simNodes.length))
  }
}

/**
 * Act on a focus another view asked for before navigating here.
 *
 * Selecting the note the user arrived from is the whole point of the file
 * viewer's "Open in memory map": landing on an unchanged, unselected graph
 * makes them hunt for the note they were already reading. Deferred to here
 * because `requestFocus` needs both a mounted canvas and placed nodes.
 */
function deliverPendingFocus() {
  const id = mm.consumePendingFocus()
  if (id) mm.requestFocus(id)
}
// The same hand-off while this view is already open: the file viewer floats
// above the route, so "Open in memory map" from a note opened here pushes a
// route that is already active and remounts nothing. `ctx` stands in for "the
// canvas is attached", which is what `deliverPendingFocus` needs.
watch(() => mm.pendingFocus, (path) => {
  if (path && ctx) deliverPendingFocus()
})
// A category/search filter change can bring previously-hidden nodes back
// into the simulation; wake it so they settle instead of sitting inert at
// whatever position they last had.
watch(() => mm.visibleIds, () => {
  refreshSimNodes()
  wakeSimulation()
})
// draw() only ever runs inside the RAF loop, which stops once the layout is
// calm (see tick()) — so changing which node is selected/on-path while the
// graph is at rest updated the reactive state but never repainted, and the
// canvas kept showing whichever node was highlighted last. A selection
// change doesn't need physics, just one more frame; waking the existing loop
// is simpler than adding a second, physics-free redraw path.
watch(() => [mm.selectedId, mm.pathStart, mm.pathEnd], () => wakeSimulation())
// Hiding orphans or showing only orphans changes *which* nodes exist in the
// layout, so the graph has to be re-framed as well as re-settled — otherwise
// filtering leaves the camera zoomed into empty space.
watch(() => mm.orphanFilter, async () => {
  await nextTick()
  refreshSimNodes()
  fitCamera()
  beginWarmup(warmupStepsFor(simNodes.length))
})
watch(canvasEl, (el) => {
  if (el) nextTick(() => attachCanvas())
})

// ---------- raw mirror for the physics loop ----------
// The layout reads x/y/vx/vy of every node against every other node, so one
// step is O(n^2) property reads — on a 552-note vault, ~600k of them. The
// store hands out Vue's deep-reactive proxies, and every one of those reads
// goes through a Proxy get trap: measured, one step costs 102ms against the
// proxies and 6.8ms against the objects they wrap. That 15x is the whole
// reason opening the memory page froze the tab — the 400-step warmup ran for
// ~40 seconds instead of ~2.7, and even the settle animation was stuck at
// ~10fps.
//
// toRaw() returns the very objects the proxies wrap, so a write here is still
// visible to anything that reads a node through the store; it just no longer
// notifies. That is what we want: nothing renders x/y except this canvas,
// which repaints itself every frame anyway.
let simNodes: MemoryGraphNode[] = []
let simById = new Map<string, MemoryGraphNode>()
function refreshSimNodes() {
  simNodes = mm.visibleNodes.map(n => toRaw(n))
  simById = new Map(simNodes.map(n => [n.id, n]))
}

/**
 * Run the layout forward toward its settled shape before handing it to the
 * cooling animation, so the graph does not visibly explode outward from random
 * starting positions.
 *
 * Spread across frames rather than run in one blocking loop. It used to be
 * synchronous "so it costs load time rather than animation time", but load
 * time is the user's time: at 6.8ms a step, a 400-step warmup is 2.7 seconds
 * with the tab wedged — no navigation, no clicks, nothing (and it was ~40s
 * before the raw-mirror fix above). Each frame now spends a slice of its
 * budget stepping and then paints, so the page stays interactive and the
 * settling is something you watch instead of something you wait out.
 */
const WARMUP_FRAME_BUDGET_MS = 8
let warmupStepsLeft = 0
let warmupStepsTotal = 0

function beginWarmup(steps: number) {
  warmupStepsTotal = Math.max(1, steps)
  warmupStepsLeft = warmupStepsTotal
  calmFrames = 0
  // Cooling starts when the warmup finishes; until then every step runs at
  // full force, as the old synchronous ramp did.
  coolingStartedAt = 0
  if (!rafId) rafId = requestAnimationFrame(tick)
}

/** One frame's worth of warmup. Returns true while more remains. */
function stepWarmupFrame(): boolean {
  const deadline = performance.now() + WARMUP_FRAME_BUDGET_MS
  do {
    stepLayout(simNodes, mm.edges, Math.max(0, warmupStepsLeft / warmupStepsTotal))
    warmupStepsLeft -= 1
  } while (warmupStepsLeft > 0 && performance.now() < deadline)
  if (warmupStepsLeft > 0) return true
  coolingStartedAt = performance.now()
  // These positions are worth keeping: the store remembers that this
  // workspace's layout is settled, so coming back skips the warmup entirely.
  mm.markGraphWarm()
  // The nodes have their final coordinates now, so a focus that arrived with
  // the navigation can fly to a position that will not move under it.
  deliverPendingFocus()
  return false
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
  // Same raw mirror the physics uses: 552 nodes plus their edges every frame
  // is another few hundred thousand property reads to keep off the proxies.
  const vis = simNodes
  const visSet = mm.visibleIds
  const highlightSet = mm.pathIds.size
    ? mm.pathIds
    : mm.selectedId
      ? new Set([mm.selectedId, ...(mm.adjacency.get(mm.selectedId) || [])])
      : null

  // Hovering a note previews its links. In a hairball the one question a dot
  // cannot answer is "what is this connected to?", and answering it only on
  // click meant losing the current selection to ask.
  const hoverId = hoveredNode.value && visSet.has(hoveredNode.value.id) ? hoveredNode.value.id : null

  ctx.lineWidth = dpr
  mm.edges.forEach(e => {
    if (!visSet.has(e.source) || !visSet.has(e.target)) return
    const a = simById.get(e.source)
    const b = simById.get(e.target)
    if (!a || !b) return
    const onPath = mm.pathIds.has(e.source) && mm.pathIds.has(e.target)
    const onHover = !!hoverId && (e.source === hoverId || e.target === hoverId)
    const dim = highlightSet && !(highlightSet.has(e.source) && highlightSet.has(e.target))
    const [ax, ay] = worldToScreen(a.x, a.y)
    const [bx, by] = worldToScreen(b.x, b.y)
    ctx!.strokeStyle = onPath
      ? '#ffd166'
      : onHover
        ? themeColors.edgeHot
        : dim
          ? themeColors.edgeDim
          : themeColors.edge
    ctx!.lineWidth = onPath ? 2.5 * dpr : onHover ? 1.8 * dpr : dpr
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
    // Staleness was reported in the list, the detail panel and the sidebar —
    // everywhere except the surface people actually look at. An amber ring
    // (the same token the "Needs review" list uses for its dot) puts "these
    // facts are unverified" on the map itself, without spending a hue that
    // the category palette needs for identity.
    if (n.stale && !dim) {
      ctx!.beginPath()
      ctx!.arc(sx, sy, r + 2.5 * dpr, 0, Math.PI * 2)
      ctx!.lineWidth = 1.4 * dpr
      ctx!.strokeStyle = themeColors.staleRing
      ctx!.stroke()
    }
    if (isSel) {
      ctx!.beginPath()
      ctx!.arc(sx, sy, r, 0, Math.PI * 2)
      ctx!.lineWidth = 2 * dpr
      ctx!.strokeStyle = themeColors.selectRing
      ctx!.stroke()
    }
  })

  // A ring under the cursor: with labels hidden at this zoom the tooltip is
  // the only name source, and the ring is what ties it to a specific dot.
  if (hoverId) {
    const n = simById.get(hoverId)!
    const [sx, sy] = worldToScreen(n.x, n.y)
    const r = nodeRadius(n) * dpr * Math.max(0.7, Math.min(1.6, camera.scale))
    ctx!.beginPath()
    ctx!.arc(sx, sy, r + 3 * dpr, 0, Math.PI * 2)
    ctx!.lineWidth = 1.5 * dpr
    ctx!.strokeStyle = themeColors.selectRing
    ctx!.stroke()
  }

  drawPulse()
  drawLabels(vis, highlightSet)
}

/**
 * One expanding, fading ring on the node a focus request landed on.
 *
 * Drawn last so it sits over the neighbours it sweeps across, and it clears
 * `pulse` itself once elapsed — the anim loop reads that to decide whether it
 * still has work.
 */
function drawPulse() {
  if (!pulse) return
  const n = simById.get(pulse.id)
  if (!n || !mm.visibleIds.has(pulse.id)) {
    pulse = null
    return
  }
  const t = (performance.now() - pulse.startedAt) / PULSE_MS
  if (t >= 1) {
    pulse = null
    return
  }
  const eased = easeOutCubic(t)
  const [sx, sy] = worldToScreen(n.x, n.y)
  const r = nodeRadius(n) * dpr * Math.max(0.7, Math.min(1.6, camera.scale))
  ctx!.beginPath()
  ctx!.arc(sx, sy, r + (3 + 26 * eased) * dpr, 0, Math.PI * 2)
  ctx!.lineWidth = Math.max(1, 2.5 * (1 - eased)) * dpr
  ctx!.strokeStyle = hexToRgba(colorForNode(n), 0.7 * (1 - eased))
  ctx!.stroke()
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
/** The canvas measures text; the truncation maths lives in graphLayout. */
function ellipsize(text: string, maxW: number): string {
  return ellipsizeTo(text, maxW, (t) => ctx!.measureText(t).width)
}

function drawLabels(vis: MemoryGraphNode[], highlightSet: Set<string> | null) {
  // Far out, nothing at all: titles only earn their pixels once the user has
  // zoomed in past LABEL_MIN_RATIO — or the view is sparse enough not to need
  // the room. Identification before that is hover's job.
  if (!labelsVisible(vis.length, zoomRatio())) return
  ctx!.font = `${LABEL_FONT_PX * dpr}px -apple-system, sans-serif`
  ctx!.textBaseline = 'middle'
  ctx!.fillStyle = themeColors.label

  const floor = labelDegreeFloor(vis.length, zoomRatio())
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
  // Warmup phase: step until this frame's slice is spent, keep the growing
  // layout framed, paint, and come back next frame.
  if (warmupStepsLeft > 0) {
    const more = stepWarmupFrame()
    fitCamera()
    draw()
    rafId = requestAnimationFrame(tick)
    if (!more) calmFrames = 0
    return
  }
  // cooling ramps 1 -> 0 over COOLING_DURATION_MS of real elapsed time; once
  // past that budget it stays at 0, which forces velocity to exactly zero
  // every subsequent step (see stepSimulation) — a hard, wall-clock bound on
  // how long this can possibly keep animating, regardless of node count,
  // frame rate, or whether the layout ever reaches a true low-energy
  // equilibrium on its own.
  const elapsed = performance.now() - coolingStartedAt
  const cooling = Math.max(0, 1 - elapsed / COOLING_DURATION_MS)
  const maxSpeed = stepLayout(simNodes, mm.edges, cooling)
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

function hitTest(wx: number, wy: number, minRadius = 0): MemoryGraphNode | null {
  return hitTestNodes(simNodes, wx, wy, minRadius)
}

// ---------- pointer gestures ----------
// One gesture model for mouse, pen and touch: Pointer Events with capture, so
// the same code drives a click, a drag and a tap on a touchscreen. The rules
// (tap slop, one pointer at a time, cancellation) live in graphGesture and are
// unit-tested; this section only maps them onto the camera and the layout.
const gesture = new GraphGesture()
let gestureNode: MemoryGraphNode | null = null
/** A touch tap's target diameter on screen, matching DESIGN.md's --touch token.
 * Enforced in world units at the current camera scale so it holds at any zoom. */
const TOUCH_HIT_DIAMETER_PX = 44

function hitTestAt(clientX: number, clientY: number, pointerType?: string): MemoryGraphNode | null {
  if (!canvasEl.value) return null
  const rect = canvasEl.value.getBoundingClientRect()
  const [wx, wy] = screenToWorld((clientX - rect.left) * dpr, (clientY - rect.top) * dpr)
  // A touch tap gets a 44 CSS-pixel target regardless of zoom, since the
  // painted node is only ~13px across at fit scale. `camera.scale` is in device
  // pixels, so the diameter is scaled by dpr to match; a 44px target must stay
  // 44 CSS px at DPR 2/3, not shrink to 22/15. Mouse/pen keep the precise
  // painted hit area.
  const minRadius = pointerType === 'touch'
    ? minHitRadiusForScale(TOUCH_HIT_DIAMETER_PX * dpr, camera.scale)
    : 0
  return hitTest(wx, wy, minRadius)
}

function onPointerDown(e: PointerEvent) {
  if (!canvasEl.value) return
  clearHover()
  const hit = hitTestAt(e.clientX, e.clientY, e.pointerType)
  const accepted = gesture.begin(
    { pointerId: e.pointerId, clientX: e.clientX, clientY: e.clientY, additive: e.shiftKey, isPrimary: e.isPrimary },
    hit ? hit.id : null,
  )
  // A second finger (or a non-primary pointer) is ignored outright: it must
  // never select a note or take over the pan the first finger started. The drag
  // target is adopted only for the accepted pointer, so an ignored press can
  // neither move the node the first finger grips nor replace it with a node
  // under the extra finger.
  if (!accepted) return
  gestureNode = hit
  // Capture routes every subsequent move/up for this pointer to the canvas even
  // if the finger leaves it, and is what lets pointercancel arrive here.
  canvasEl.value.setPointerCapture?.(e.pointerId)
  e.preventDefault()
}

function onPointerMove(e: PointerEvent) {
  if (!gesture.active) return
  const move = gesture.move({
    pointerId: e.pointerId,
    clientX: e.clientX,
    clientY: e.clientY,
    additive: false,
    isPrimary: e.isPrimary,
  })
  if (!move) return
  if ('dragging' in move) {
    const node = gestureNode
    if (!node) return
    wakeSimulation()
    if (!canvasEl.value) return
    const rect = canvasEl.value.getBoundingClientRect()
    const [wx, wy] = screenToWorld((e.clientX - rect.left) * dpr, (e.clientY - rect.top) * dpr)
    node.x = wx
    node.y = wy
    node.vx = 0
    node.vy = 0
    return
  }
  // Dragging is the user taking the camera: whatever tween was in flight would
  // otherwise keep pulling the view out from under the gesture.
  cancelCameraTween()
  camera.x += move.pan.dx * dpr
  camera.y += move.pan.dy * dpr
  requestRedraw()
}

/** One move handler on the canvas: a live gesture drags/pans, an idle pointer
 * hovers. Keeping them separate listeners would double-fire on a drag. */
function onCanvasPointerMove(e: PointerEvent) {
  if (gesture.active) onPointerMove(e)
  else onCanvasHover(e)
}

function onPointerUp(e: PointerEvent) {
  // Only the pointer that owns the gesture may end it and release the target;
  // an ignored pointer's `pointerup` must not clear the drag the first finger
  // is still performing.
  if (gesture.activePointerId !== e.pointerId) return
  const result = gesture.end({ pointerId: e.pointerId })
  gestureNode = null
  if (result.tap === 'node') mm.handleNodeClick(result.nodeId, result.additive)
  else if (result.tap === 'empty') mm.selectNode(null)
}

// `pointercancel` is the browser taking the touch for a scroll/zoom/system
// gesture; `lostpointercapture` is the capture ending for any reason. Either
// way the drag must stop cleanly rather than leave a node stuck under a finger.
function onPointerCancel(e: PointerEvent) {
  if (gesture.cancel({ pointerId: e.pointerId })) {
    gestureNode = null
  }
}
function onLostPointerCapture(e: PointerEvent) {
  if (gesture.cancel({ pointerId: e.pointerId })) {
    gestureNode = null
  }
}

// ---------- hover name overlay ----------
// With labels hidden at far-out zoom (the default framing of a real vault),
// a node's name has to be one hover away or the graph is unidentifiable.
// DOM overlay rather than canvas text so it can follow the cursor without
// waking the render loop for every pixel of movement; only the enter/leave
// of a node redraws (the highlight ring).
const hoveredNode = ref<MemoryGraphNode | null>(null)
const hoverPos = ref({ x: 0, y: 0 })
function onCanvasHover(e: PointerEvent) {
  // While a press is held (node drag, pan) the surface is "grabbed", not
  // "pointing" — a tooltip chasing the cursor mid-drag reads as noise. Coarse
  // pointers have no hover at all, so this only ever matters for a mouse/pen.
  if (!canvasEl.value || gesture.active) {
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

function onWheel(e: WheelEvent) {
  cancelCameraTween()
  if (!canvasEl.value || !W || !H) return
  const rect = canvasEl.value.getBoundingClientRect()
  // Cursor in device pixels, same coordinate space as W/H and worldToScreen.
  const sx = (e.clientX - rect.left) * dpr
  const sy = (e.clientY - rect.top) * dpr
  const [wx, wy] = screenToWorld(sx, sy)
  const delta = -e.deltaY * 0.0012
  const newScale = clampScale(camera.scale * (1 + delta))
  if (newScale === camera.scale) return
  // Keep the world point under the cursor fixed while the scale changes.
  camera.scale = newScale
  camera.x = sx - W / 2 - wx * newScale
  camera.y = sy - H / 2 - wy * newScale
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
const vaultReview = useVaultReviewStore()
// Tab badges, scoped like the lists they count: the workspace toggle scopes
// both queues, so a global tally would claim rows the tab will not show.
const proposalCount = computed(() => proposals.scopedRows(store.activeWorkspace).length)
// Candidates only. The tally used to add the trash in, so a queue with nothing
// left to decide still wore a badge counting notes already retired — a number
// asking for attention no click could clear. Retired notes carry their own
// count on their own tab, which is where a "how much is in there" number
// belongs.
const retirementCount = computed(() =>
  vaultReview.loadedWorkspace === store.activeWorkspace ? vaultReview.candidates.length : 0,
)
const trashCount = computed(() =>
  vaultReview.loadedWorkspace === store.activeWorkspace ? vaultReview.trashed.length : 0,
)
// Null until the ledger has loaded, and the badge stays hidden while it is: a
// count rendered before the fetch read "History 0" on a ledger with hundreds
// of rows. Unfiltered it reports the server's scoped total rather than the
// rows we happen to hold, because the page is capped.
const historyCount = computed(() => {
  if (!proposals.historyLoaded) return null
  if (proposals.historyFiltersActive) return proposals.visibleHistory(store.activeWorkspace).length
  return proposals.historyTotal
})

/** Which of Review's four sections is on screen.
 *
 * Derived rather than stored: the underlying state (`mm.reviewTab`,
 * `mm.retirementTab`, `proposals.view`) is what every other entry point sets,
 * so flattening the navigation may not fork it into a fifth source of truth
 * that those entry points would have to learn about.
 */
type ReviewTabKey = 'proposals' | 'retirement' | 'trash' | 'history'
const reviewTab = computed<ReviewTabKey>({
  get() {
    if (mm.reviewTab === 'retirement') return mm.retirementTab === 'trash' ? 'trash' : 'retirement'
    return proposals.view === 'history' ? 'history' : 'proposals'
  },
  set(key) {
    if (key === 'retirement' || key === 'trash') {
      mm.reviewTab = 'retirement'
      mm.retirementTab = key === 'trash' ? 'trash' : 'candidates'
      return
    }
    mm.reviewTab = 'proposals'
    proposals.view = key === 'history' ? 'history' : 'queue'
    if (key === 'history') void proposals.ensureHistoryLoaded(store.activeWorkspace)
  },
})

// Labels name the decision, not the machinery. "Proposals" and "Retirements"
// described the pipeline that produced the rows; these say what is in them.
const reviewTabs = computed<TabSpec<ReviewTabKey>[]>(() => [
  // `|| undefined` rather than 0: a zero pill on an empty queue is noise.
  { key: 'proposals', label: 'Suggested memories', count: proposalCount.value || undefined },
  { key: 'retirement', label: 'Notes to revisit', count: retirementCount.value || undefined },
  { key: 'trash', label: 'Retired', count: trashCount.value || undefined },
  { key: 'history', label: 'History', count: historyCount.value },
])

/** Switch the map's rendering. Both live at /memory, so there is no route to
 * push — only `mapView` to remember, so leaving Review comes back here. */
function setMapView(next: 'graph' | 'list') {
  mm.view = next
  mm.mapView = next
}

// A stale note's detail panel lands directly on the retirement queue.
function openRetirementReview() {
  mm.reviewTab = 'retirement'
  mm.retirementTab = 'candidates'
  mm.view = 'review'
  if (router.currentRoute.value.path !== '/proposals') void router.push('/proposals')
}
// The badges need data even while the Retirement tab (which fetches on mount)
// has never been opened. Joining the in-flight request when the panel mounts
// keeps this to one GET.
//
// `immediate` matters: `mm.view` lives in the store, so remounting this
// component while it already reads 'review' (arriving at /proposals from a
// page that unmounted us, after the sidebar set the view) makes the seeding
// assignment a no-op and a plain watcher would never fire — leaving both tab
// badges at 0 until the user clicks Retirement.
//
// The seeding runs FIRST so `immediate` sees the view this mount will
// actually show. Registering the watcher first instead fires it against the
// previous mount's leftover 'review' (landing on /memory by URL or the back
// button), paying for both of the app's heaviest reads a line before the
// seeding switches to 'graph'.
// `mm.mapView`, not a literal 'graph': the drawing is a remembered preference
// now, so arriving at /memory from anywhere restores the one last used instead
// of snapping every visit back to the canvas.
mm.view = router.currentRoute.value.path.startsWith('/proposals') ? 'review' : mm.mapView
watch(() => mm.view, (view) => {
  if (view === 'review') {
    void proposals.ensureLoaded()
    // The History tab's count now lives on the shared bar, which is on screen
    // whichever section is showing — so the ledger has to be loaded even when
    // the panel that used to prefetch it is not mounted.
    if (store.activeWorkspace) {
      void proposals.ensureHistoryLoaded(store.activeWorkspace)
      void vaultReview.ensureLoaded(store.activeWorkspace)
    }
  }
}, { immediate: true })
const sortKey = ref<'title' | 'type' | 'degree' | 'age'>('title')
const sortDir = ref(1)
type SortKey = 'title' | 'type' | 'degree' | 'age'
function setSort(key: SortKey) {
  if (sortKey.value === key) sortDir.value *= -1
  // Age opens oldest-first: the point of sorting by age is triage, and a
  // larger ageDays is an older note, so that is the descending direction.
  else { sortKey.value = key; sortDir.value = key === 'age' ? -1 : 1 }
}
function sortCaret(key: SortKey): string {
  if (sortKey.value !== key) return ''
  return sortDir.value > 0 ? '\u25b4' : '\u25be'
}
function ariaSort(key: SortKey): 'ascending' | 'descending' | 'none' {
  if (sortKey.value !== key) return 'none'
  return sortDir.value > 0 ? 'ascending' : 'descending'
}
/** Link count as a share of the busiest visible note, for the list's bar. */
const maxVisibleDegree = computed(() => mm.visibleNodes.reduce((m, n) => Math.max(m, n.degree), 0))
function degreeBarPct(n: MemoryGraphNode): number {
  if (!maxVisibleDegree.value) return 0
  return Math.round((n.degree / maxVisibleDegree.value) * 100)
}
/** Age for the detail heading; empty when the note carries no date at all. */
const ageLabelOfSelected = computed(() => {
  const n = mm.selectedNode
  if (!n) return ''
  return mm.ageLabelOf(n)
})

// Which control opened the note currently in the detail panel, and the element
// to return focus to. Closing the panel returns focus there — otherwise a
// keyboard user who opened a note and pressed the close button landed back at
// the top of the document, having to tab through the whole list to get where
// they were.
//
// The element is captured by reference rather than looked up by id, because
// opening a note from a neighbor link unmounts that link: the panel's content
// is replaced with the neighbor's, so the `[data-mm-return]` node for it no
// longer exists by the time the panel closes. When the initiating control is
// gone (the graph-view neighbor case), focus falls back to the graph region,
// which is a stable, always-mounted destination in the same surface.
const detailReturnFocus = ref<{ id: string; el: HTMLElement | null } | null>(null)
/** Resolve the control to restore focus to: the clicked button itself, or the
 * title button inside a clicked row. */
function returnControlFor(event: Event): HTMLElement | null {
  const target = event.currentTarget as HTMLElement | null
  if (!target) return null
  if (target.matches('button')) return target
  return target.querySelector<HTMLElement>('[data-mm-return]')
}
function activateRow(n: MemoryGraphNode, event: Event) {
  detailReturnFocus.value = { id: n.id, el: returnControlFor(event) }
  mm.selectNode(n.id)
}
function closeDetail() {
  mm.selectNode(null)
}
watch(() => mm.selectedId, (id) => {
  if (id !== null) {
    // The selection moves through paths that never record a return control: a
    // canvas tap (`mm.handleNodeClick`), `focusNode`, `openNoteFile`,
    // `deliverPendingFocus` and the sidebar's `mm.requestFocus`. An entry left
    // over from an earlier note is stale the moment one of those lands — on
    // close, focus would jump to the other note's button while it is still
    // mounted (list view), or the fallback would re-find that other note's
    // title. Only the control that opened the note actually on screen may
    // claim the restore, so the stale element is dropped — but the entry is
    // re-pointed at the new note rather than cleared, so the fallback below
    // still lands on that note's own title (or the surface region) instead of
    // letting focus fall to the document body mid-journey.
    if (detailReturnFocus.value && detailReturnFocus.value.id !== id) {
      detailReturnFocus.value = { id, el: null }
    }
    return
  }
  if (!detailReturnFocus.value) return
  const { id: returnId, el } = detailReturnFocus.value
  detailReturnFocus.value = null
  void nextTick(() => {
    if (el && el.isConnected) {
      el.focus()
      return
    }
    // The initiating link is gone (graph-view neighbor navigation). Prefer the
    // list's title button when this note has one on screen, else the active
    // surface's own region, so focus never drops to the document body.
    const listTitle = Array.from(document.querySelectorAll<HTMLElement>('[data-mm-return]'))
      .find(candidate => candidate.dataset.mmReturn === returnId)
    if (listTitle) listTitle.focus()
    else document.querySelector<HTMLElement>('.mm-canvas-wrap, .mm-list-wrap')?.focus()
  })
})
// Hoisted: constructing a collator per comparison is roughly an order of
// magnitude slower than `<`, and this list re-sorts on every search keystroke.
const listCollator = new Intl.Collator(undefined, { sensitivity: 'base' })

// The number of days the "Checked" cell is actually showing. `ageLabelOf`
// falls back to mtime when a note has no `updated:` frontmatter, so sorting on
// `ageDays` alone pinned a note displaying "3y" below one displaying "2mo" —
// the same display/sort mismatch fixed for the Type column, on exactly the
// stale notes triage exists to surface. Only a node with nothing to show at
// all (`ageLabelOf` returns '') sorts last.
function sortableAgeDays(n: MemoryGraphNode): number | null {
  if (!mm.ageLabelOf(n)) return null
  return n.ageDays ?? Math.floor((Date.now() / 1000 - n.mtime) / 86400)
}

const sortedVisibleNodes = computed(() => {
  const key = sortKey.value
  const dir = sortDir.value
  // Precomputed once per node rather than per comparison: categoryLabelFor
  // walks into personSubtype, which allocates a Set from the node's tags.
  const decorated = mm.visibleNodes.map((n) => ({
    n,
    label: key === 'type' ? categoryLabelFor(n) : '',
    age: key === 'age' ? sortableAgeDays(n) : null,
  }))
  decorated.sort((x, y) => {
    let av: string | number
    let bv: string | number
    if (key === 'degree') { av = x.n.degree; bv = y.n.degree }
    else if (key === 'age') {
      // Undated notes sort last in BOTH directions. A MAX_SAFE_INTEGER
      // sentinel only held while ascending, so the second click — the one
      // that actually gives oldest-first triage — floated them above the
      // genuinely stale ones it was meant to surface.
      if (x.age === null && y.age === null) return 0
      if (x.age === null) return 1
      if (y.age === null) return -1
      av = x.age; bv = y.age
    } else if (key === 'type') {
      av = x.label; bv = y.label
    } else {
      // Every other key has been eliminated above, so `key` narrows to
      // 'title' and this indexes MemoryGraphNode as a plain string — no cast.
      av = x.n[key] || ''; bv = y.n[key] || ''
    }
    if (typeof av === 'string' && typeof bv === 'string') {
      // Collator, not `<`: raw comparison orders by code point, so every
      // capitalised title sorts ahead of every lowercase one ('Zebra' < 'apple')
      // and a vault mixing both spellings reads as unsorted.
      return listCollator.compare(av, bv) * dir
    }
    if (av < bv) return -1 * dir
    if (av > bv) return 1 * dir
    return 0
  })
  return decorated.map((d) => d.n)
})

// ---------- lifecycle ----------
onMounted(async () => {
  // ensureGraph, not loadGraph: a workspace already in the store's snapshot
  // cache paints immediately and revalidates in the background, so returning
  // to this page costs no skeleton and no re-layout.
  await mm.ensureGraph(store.activeWorkspace)
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
  if (animRafId) cancelAnimationFrame(animRafId)
  camTween = null
  pulse = null
  gesture.cancel()
  ro?.disconnect()
  window.removeEventListener('mousemove', handleDetailDrag)
  window.removeEventListener('mouseup', stopDetailDrag)
  window.removeEventListener('resize', onResizeNarrow)
  document.body.classList.remove('is-dragging-layout')
})
</script>

<style scoped>
.memory-map {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
/* The Review surface holds all four sections — two queues waiting on a
   decision and two records of decisions already made — under one tab bar. */
.mm-review-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* Layout only. The tab styling itself lives in TabBar, which this bar renders
   through. It is now the only tab row on the Review surface; at a phone width
   the four tabs scroll sideways inside it rather than wrapping, so the panel
   below always starts at the same height. */
.mm-review-tabs {
  padding: 0 var(--space-4);
  flex: none;
}
/* A stale note's way into the retirement queue, next to its last-verified
   line. A text button, not a pink bar: deciding happens in Review, not here. */
.mm-detail-review-link {
  background: none;
  border: none;
  padding: 0;
  min-height: var(--touch);
  font-family: var(--font);
  font-size: var(--text-xs);
  color: var(--accent);
  text-align: left;
  cursor: pointer;
}
.mm-detail-review-link:hover { text-decoration: underline; }
.mm-detail-review-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.mm-body {
  flex: 1;
  min-height: 0;
  display: grid;
  /* Same split as the chat's pinned-file panel: hidden takes no space at
     all, so the graph gets the full width until a note is selected. */
  grid-template-columns: 1fr;
}
.mm-body.mm-body--detail-open {
  grid-template-columns: 1fr 320px;
}
.mm-body--dragging-detail {
  user-select: none;
}
.mm-body--dragging-detail .mm-detail {
  transition: none;
}
@media (max-width: 900px) {
  .mm-body.mm-body--detail-open { grid-template-columns: 1fr; }
  .mm-detail { display: none; }
  .mm-detail-resizer { display: none; }
}

.mm-detail {
  position: relative;
  overflow-y: auto;
  padding: var(--space-3);
  background: var(--bg2);
  border-left: 1px solid var(--border);
  /* The panel is created by v-if, so a mount animation is all it needs. It
     used to appear instantly, which reads as the layout jumping rather than a
     panel opening — the graph column resizes at the same moment. */
  animation: mm-detail-in 180ms ease-out;
}
@keyframes mm-detail-in {
  from { opacity: 0; transform: translateX(10px); }
  to { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .mm-detail { animation: none; }
}
.mm-detail-resizer {
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  width: 6px;
  margin-left: -3px;
  cursor: col-resize;
  z-index: 2;
  touch-action: none;
}
.mm-detail-resizer::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 1px;
  background: transparent;
  transition: background 120ms;
}
.mm-detail-resizer:hover::after,
.mm-body--dragging-detail .mm-detail-resizer::after {
  background: var(--accent);
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
/* A neighbor row wraps its action group below the title at narrow widths
   instead of overflowing horizontally. */
.mm-link-item {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 5px 6px; border-radius: var(--radius-sm);
  font-size: var(--text-sm); color: var(--fg);
}
.mm-link-item:hover { background: var(--bg3); }
.mm-link-item .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; align-self: center; }
.mm-link-item .cnt { margin-left: auto; color: var(--fg3); }
/* The neighbor's name is a real button: opening it is a core action, and a
   clickable span is unreachable by keyboard. Reset to read like the text it
   replaced, then give it the app's focus ring. `min-width: 0` lets a long title
   shrink and wrap; the actions keep their own row when there is no room. */
.mm-link-btn {
  background: none; border: none; padding: 0; margin: 0; text-align: left;
  font: inherit; color: inherit; cursor: pointer; min-height: var(--touch);
  display: flex; align-items: center; flex: 1 1 auto; min-width: 0;
  overflow-wrap: anywhere;
}
.mm-link-actions { display: flex; align-items: center; gap: 4px; flex: 0 0 auto; }
.mm-link-label { cursor: pointer; }
.mm-link-label:hover { text-decoration: underline; }
.mm-link-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.mm-link-focus {
  background: none; border: none; color: var(--fg3); cursor: pointer; padding: 2px 4px;
  border-radius: var(--radius-sm); font-size: var(--text-sm); line-height: 1; flex: none;
  min-width: var(--touch); min-height: var(--touch);
}
.mm-link-focus:hover { background: var(--bg2); color: var(--fg); }

.mm-canvas-wrap { position: relative; overflow: hidden; background: var(--bg); flex: 1; min-height: 0; }
/* touch-action:none makes the canvas own its touches so a drag pans instead of
   scrolling the page underneath. It is scoped to the canvas on purpose: panning
   and pinch-zoom of the page remain available everywhere else, per DESIGN.md. */
.mm-canvas-wrap canvas {
  display: block; width: 100%; height: 100%; cursor: grab;
  touch-action: none; -webkit-user-select: none; user-select: none;
}
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
/* Path endpoints for the focused note, without requiring a shift-click on a
   dot (impossible on touch, undiscoverable without a mouse). Sits under the
   zoom controls so the two never overlap. */
.mm-path-controls {
  position: absolute; top: calc(var(--space-3) + 3 * var(--touch) + 12px); right: var(--space-3);
  display: flex; flex-direction: column; gap: 4px; align-items: flex-end;
  max-width: 200px;
}
.mm-path-controls-label {
  font-size: var(--text-xs); color: var(--fg3); text-align: right;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px;
}
.mm-path-controls .btn-chip { min-height: var(--touch); }
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
  stroke-width: 2;
  stroke-linecap: round;
  opacity: 0.55;
}
.mm-brain-node {
  fill: var(--bg3);
  stroke: var(--border);
  stroke-width: 1.6;
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

.mm-list-wrap { overflow: auto; padding: var(--space-4); flex: 1; min-height: 0; }
.mm-list-wrap:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.mm-list-wrap table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
.mm-list-wrap thead th {
  text-align: left; padding: 0; color: var(--fg3); font-weight: 500; font-size: var(--text-xs);
  text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid var(--border);
  position: sticky; top: 0; background: var(--bg); z-index: 1;
}
/* Sortable headers put their padding on the button so the whole cell is the
   hit target; the one non-sortable header keeps it on the cell. */
.mm-list-wrap thead th.th-plain { padding: 6px 10px; }
.mm-sort {
  display: flex; align-items: center; gap: 4px; width: 100%;
  background: none; border: none; padding: 6px 10px; cursor: pointer;
  font: inherit; color: inherit; text-transform: inherit; letter-spacing: inherit; text-align: left;
}
.mm-sort:hover { color: var(--fg2); }
.mm-sort:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }
.mm-sort-caret { font-size: 9px; line-height: 1; color: var(--accent); }
.mm-list-wrap tbody td { padding: 6px 10px; border-bottom: 1px solid var(--bg3); }
.mm-list-wrap tbody tr { cursor: pointer; }
.mm-list-wrap tbody tr:hover { background: var(--bg3); }
/* The row whose note the detail panel is showing. Without it, clicking a row
   opened the panel with no indication of which row it came from. */
.mm-list-wrap tbody tr.current { background: var(--bg2); box-shadow: inset 2px 0 0 var(--accent); }
.mm-list-wrap .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 7px; }
.mm-list-wrap .muted { color: var(--fg3); }
/* The note title is a button so the row's action is keyboard reachable. It
   reads as the plain text it replaced, and carries a full 44px hit target. */
.cell-title { padding-top: 0; padding-bottom: 0; }
.mm-title-btn {
  display: flex; align-items: center; width: 100%; min-height: var(--touch);
  background: none; border: none; padding: 0; margin: 0; text-align: left;
  font: inherit; color: inherit; cursor: pointer;
}
.mm-title-btn:hover { text-decoration: underline; }
.mm-title-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.path-cell { white-space: nowrap; }
.mm-path-btn {
  background: none; border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--fg3); font-family: var(--font); font-size: var(--text-xs);
  min-height: var(--touch); min-width: 44px; padding: 0 8px; margin-right: 4px; cursor: pointer;
}
.mm-path-btn:hover { color: var(--fg); border-color: var(--fg2); }
.mm-path-btn.active { color: var(--accent); border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
/* Inside the neighbor row the flex gap owns spacing, so drop the trailing
   margin that the list cell needs. */
.mm-link-actions .mm-path-btn { margin-right: 0; }
.mm-path-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.deg-cell { white-space: nowrap; }
.deg-bar {
  display: inline-block; width: 46px; height: 4px; border-radius: 2px;
  background: var(--bg3); overflow: hidden; vertical-align: middle; margin-right: 7px;
}
.deg-bar > span {
  display: block; height: 100%; border-radius: 2px;
  background: color-mix(in srgb, var(--accent) 70%, transparent);
}
.deg-n { color: var(--fg2); font-variant-numeric: tabular-nums; }
.stale-age { color: var(--warning, #ff9800); white-space: nowrap; }
.stale-flag {
  display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: var(--radius-pill);
  background: color-mix(in srgb, var(--warning, #ff9800) 15%, transparent);
  font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.04em;
}
.tag-mini { display: inline-block; background: var(--bg3); color: var(--fg2); border-radius: 4px; padding: 1px 6px; font-size: var(--text-xs); margin: 0 3px 2px 0; }

.mm-detail-type { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--fg3); }
.mm-detail-title { font-size: var(--text-lg); font-weight: 600; margin: 4px 0 var(--space-2); }
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
.mm-detail-path-controls {
  display: flex; flex-wrap: wrap; gap: var(--space-2);
  margin: 0 0 var(--space-2);
}
.mm-detail-path-controls .btn-chip { min-height: var(--touch); }
.mm-detail-path-hint { color: var(--fg3); font-size: var(--text-xs); margin: 0 0 var(--space-2); }
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
.mm-seg button.active { background: var(--accent); color: var(--on-accent); }

/* Canvas toolbar: overlays the graph top-left, opposite the zoom controls.
   Wraps rather than scrolls so a narrow window stacks the groups instead of
   hiding the orphan toggle off the edge. */
/* The map's own column: a toolbar row above whichever rendering is showing.
   The two renderings take the remaining height (`min-height: 0` so the canvas
   can shrink and the list can scroll rather than stretching the page). */
.mm-surface {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
/* Static, not absolutely positioned over the canvas: the row is shared with
   the list now, where an overlay would sit on top of the table header. The
   `max-width` that used to keep it clear of the zoom controls goes with it. */
.mm-toolbar {
  flex: none;
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.mm-seg--sm button { padding: 4px 10px; font-size: var(--text-xs); }
.mm-toggle {
  background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--fg2); font-family: var(--font); font-size: var(--text-xs); padding: 4px 10px; cursor: pointer;
}
.mm-toggle.on { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
</style>
