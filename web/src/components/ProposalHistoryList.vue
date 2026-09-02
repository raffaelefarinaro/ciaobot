<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useProposalsStore } from '../stores/proposals'
import { useProjectStore } from '../stores/projects'
import type { ProposalHistoryRow } from '../lib/types'
import { kindLabel } from '../lib/proposalKinds'
import { formatTime } from '../lib/time'

const store = useProposalsStore()
const projectStore = useProjectStore()

onMounted(() => {
  void store.ensureHistoryLoaded(projectStore.activeWorkspace)
})

// The server scopes the page to one workspace, so a switch needs a new request
// rather than a re-filter of rows that no longer cover the active workspace.
watch(() => projectStore.activeWorkspace, ws => {
  void store.ensureHistoryLoaded(ws)
})

const ACTION_FILTERS: { key: 'all' | 'accepted' | 'dismissed'; label: string }[] = [
  { key: 'all', label: 'all' },
  { key: 'accepted', label: 'accepted' },
  { key: 'dismissed', label: 'dismissed' },
]

const ACTOR_FILTERS: { key: 'all' | 'pwa' | 'agent' | 'auto'; label: string }[] = [
  { key: 'all', label: 'anyone' },
  { key: 'pwa', label: 'you' },
  { key: 'agent', label: 'agent' },
  { key: 'auto', label: 'automatic' },
]

const filteredRows = computed(() => store.visibleHistory(projectStore.activeWorkspace))

/** Who decided, in the same words the row's actor badge uses. */
function actorLabel(via: string): string {
  if (via === 'pwa') return 'you'
  if (via === 'agent') return 'agent'
  if (via === 'auto') return 'automatic'
  return 'unknown'
}

/** Status badge class + text. `outcome` overrides a plain accept/dismiss
 * label when the row was not a fresh write: already-known facts the
 * archive-time auto-promoter recognized, and rows an expiry sweep dropped.
 */
function statusBadge(row: ProposalHistoryRow): { cls: string; text: string } {
  if (row.action === 'accepted') {
    if (row.outcome === 'duplicate' || row.outcome === 'suppressed') {
      return { cls: 'badge--muted', text: 'Skipped · already known' }
    }
    return { cls: 'badge--success', text: 'Accepted' }
  }
  if (row.outcome === 'swept') {
    return { cls: 'badge--muted', text: 'Dismissed · expired' }
  }
  return { cls: 'badge--muted', text: 'Dismissed' }
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
}

/** The bucket a row belongs to. Undated legacy rows, and rows whose `ts` does
 * not parse, share one "earlier" bucket. */
function dayKey(ts: string): string {
  if (!ts) return 'earlier'
  const d = new Date(ts)
  return isNaN(d.getTime()) ? 'earlier' : d.toDateString()
}

function dayLabel(ts: string): string {
  if (!ts) return 'Earlier'
  const d = new Date(ts)
  if (isNaN(d.getTime())) return 'Earlier'
  const diffDays = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000)
  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
}

/** Day buckets in first-seen order. Keyed into a map rather than appended
 * while a key repeats: the server sorts by the raw timestamp string, so a
 * single unparseable legacy `ts` is not contiguous with the other undated
 * rows and produced several interleaved "Earlier" sections.
 */
const groups = computed(() => {
  const byKey = new Map<string, { key: string; label: string; rows: ProposalHistoryRow[] }>()
  for (const row of filteredRows.value) {
    const key = dayKey(row.ts)
    const group = byKey.get(key)
    if (group) group.rows.push(row)
    else byKey.set(key, { key, label: dayLabel(row.ts), rows: [row] })
  }
  return [...byKey.values()]
})

/** Whether anything is narrowing the list. The kind chips and the search box
 * are shared with the queue tab, whose "clear filter" control is on that tab,
 * so without a reset here a filter set over there silently emptied this list. */
// Shared with the panel's tab badge, which must not report a filtered count
// as the ledger total.
const filtersActive = computed(() => store.historyFiltersActive)

/** Distinguishes "nothing recorded" from "nothing matches the filters". */
const filtersHideEverything = computed(
  () => !filteredRows.value.length
    && store.scopedHistory(projectStore.activeWorkspace).length > 0,
)
</script>

<template>
  <div class="ph-list">
    <p class="ph-hint">
      Decisions already made: what you accepted or dismissed, what the nightly
      agent filed, and what archiving added — or recognized as already known —
      on its own.
    </p>

    <div class="ph-filters">
      <div class="ph-chip-row">
        <button
          v-for="f in ACTION_FILTERS"
          :key="f.key"
          type="button"
          class="btn-small btn-chip"
          :class="{ active: store.historyActionFilter === f.key }"
          @click="store.historyActionFilter = f.key"
        >{{ f.label }}</button>
      </div>
      <div class="ph-chip-row">
        <button
          v-for="f in ACTOR_FILTERS"
          :key="f.key"
          type="button"
          class="btn-small btn-chip"
          :class="{ active: store.historyActorFilter === f.key }"
          @click="store.historyActorFilter = f.key"
        >{{ f.label }}</button>
        <button
          v-if="filtersActive"
          type="button"
          class="ph-clear-filter"
          @click="store.resetFilters()"
        >clear filters</button>
      </div>
    </div>

    <p v-if="store.historyError" class="ph-error" role="alert">{{ store.historyError }}</p>

    <p v-if="store.historyLoading && !store.historyLoaded" class="ph-empty">Loading…</p>
    <!-- A failed fetch shows the error alone. Falling through to the empty
         states printed "No decisions yet." under the error, which claims an
         empty ledger when the ledger merely could not be read. -->
    <template v-else-if="store.historyError && !store.historyLoaded" />
    <p v-else-if="filtersHideEverything" class="ph-empty">No decisions match the current filters.</p>
    <p v-else-if="!filteredRows.length" class="ph-empty">No decisions yet.</p>

    <template v-else>
      <section v-for="group in groups" :key="group.key" class="ph-group">
        <header class="ph-group-head">
          <span class="ph-group-label">{{ group.label }}</span>
          <span class="ph-group-count">{{ group.rows.length }}</span>
        </header>
        <ul class="ph-rows">
          <li v-for="row in group.rows" :key="row.id" class="ph-row">
            <div class="ph-row-top">
              <span class="ph-kind">{{ kindLabel(row.kind) }}</span>
              <span class="badge" :class="statusBadge(row).cls">{{ statusBadge(row).text }}</span>
              <span class="ph-actor">{{ actorLabel(row.via) }}</span>
              <span v-if="row.ts" class="ph-time">{{ formatTime(row.ts) }}</span>
            </div>
            <p class="ph-text">{{ row.text }}</p>
            <p v-if="row.destination" class="ph-destination">{{ row.destination }}</p>
            <details v-if="row.source" class="ph-source">
              <summary>details</summary>
              <p>from {{ row.source }}</p>
            </details>
          </li>
        </ul>
      </section>
    </template>

    <!-- Pagination sits outside the empty-state chain above. The filters are
         client-side over the page we hold, so a filter matching only rows
         older than the page has to stay able to reach them: nesting this in
         the `v-else` printed "No decisions match" with no way to load the
         rows that do. -->
    <template v-if="store.historyLoaded && !store.historyError">
      <button
        v-if="store.historyCanLoadMore"
        type="button"
        class="btn-small btn-chip ph-more"
        :disabled="store.historyLoading"
        @click="store.loadMoreHistory()"
      >{{ store.historyLoading ? 'loading…' : 'show more' }}</button>
      <p v-else-if="store.historyAtMax && store.historyTruncated" class="ph-capped">
        Showing the newest {{ store.historyLimit }} of {{ store.historyTotal }} decisions.
      </p>
    </template>
  </div>
</template>

<style scoped>
.ph-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.ph-hint {
  color: var(--fg2);
  font-size: 0.85rem;
  margin: 0;
}

.ph-filters {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.ph-chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.ph-empty {
  color: var(--fg2);
  font-size: 0.9rem;
  padding: var(--space-4) 0;
}

.ph-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.ph-group-head {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  color: var(--fg2);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.ph-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.ph-row {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space-2) var(--space-3);
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.ph-row-top {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.ph-kind {
  flex: none;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  background: var(--bg3);
  color: var(--fg2);
}

.ph-actor {
  color: var(--fg2);
  font-size: 0.8rem;
}

.ph-time {
  margin-left: auto;
  color: var(--fg2);
  font-size: 0.75rem;
  font-variant-numeric: tabular-nums;
}

.ph-text {
  margin: 0;
  font-size: 0.9rem;
}

.ph-destination {
  margin: 0;
  color: var(--fg2);
  font-size: 0.8rem;
  font-family: var(--font-mono, ui-monospace, monospace);
}

.ph-source {
  color: var(--fg2);
  font-size: 0.8rem;
}
.ph-source summary {
  cursor: pointer;
}
.ph-source p {
  margin: 0.2rem 0 0;
}

.ph-more {
  align-self: flex-start;
}

.ph-capped {
  margin: 0;
  color: var(--fg2);
  font-size: 0.8rem;
}

.ph-error {
  margin: 0;
  color: var(--error);
  font-size: 0.85rem;
}

/* Same affordance as the queue tab's "clear filter", which is on the other
   tab: the kind chips and search are shared, so the reset has to be reachable
   from whichever tab the filter is hiding rows on. */
.ph-clear-filter {
  margin-left: var(--space-2);
  background: none;
  border: none;
  padding: 0;
  color: var(--accent);
  font-size: 0.78rem;
  cursor: pointer;
}

/* Touch: the link is only glyph-height, well under the 44px minimum. Grow the
   hit area with padding and pull the extra back with a matching negative
   margin, so the control stays visually inline where it sits next to the
   "no matches" text. Same trick as the chat message actions. Desktop keeps
   the tight inline link. */
@media (pointer: coarse) {
  .ph-clear-filter {
    --ph-clear-visual: 1.1rem;
    --ph-clear-pad: calc((var(--touch, 44px) - var(--ph-clear-visual)) / 2);
    display: inline-block;
    padding: var(--ph-clear-pad);
    margin: calc(-1 * var(--ph-clear-pad));
    margin-left: calc(var(--space-2) - var(--ph-clear-pad));
  }
}
</style>
