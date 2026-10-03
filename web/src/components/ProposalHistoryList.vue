<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useProposalsStore } from '../stores/proposals'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import type { ProposalHistoryRow } from '../lib/types'
import { kindLabel } from '../lib/proposalKinds'
import { formatTime } from '../lib/time'

const store = useProposalsStore()
const projectStore = useProjectStore()
const fileViewer = useFileViewerStore()

// -- Changes and undo -------------------------------------------------------
//
// History said what was decided and where it landed, never what the decision
// actually did to the destination. A row carries a `change` pointer when the
// receipt protocol performed it; the before/after images are fetched per row,
// on expand, because a page of 200 decisions would otherwise ship 200 bodies
// nobody opened.
//
// A row with NO `change` is one the protocol never recorded — every decision
// made before receipts landed, and every one made outside them. Those say "No
// change snapshot available" and get no Undo: claiming every historical edit
// is reversible would be a lie the undo path then has to refuse.
const openChangeIds = ref<Set<string>>(new Set())

/** What a filed note edit would have done, in words.
 *
 * `retire` is the one whose name is a lie on its own: the operation moved the
 * note and wrote no text, so calling it "retire" without saying so reads as a
 * rewrite. The rest are close enough to their wire names to show them.
 *
 * The three entry operations are all past tense here, unlike the note ones,
 * because a history row records a decision that already happened — and each
 * one names the *unit*, since "would rewrite the whole note" about an accept
 * that changes a single bullet is the exact claim this table exists to avoid. */
const OPERATION_WORDS: Record<string, string> = {
  replace: 'would rewrite the whole note',
  restamp: 'would re-stamp the verification date',
  retire: 'would move the note to Retired',
  replace_entry: 'rewrote one fact inside the note',
  restamp_entry: 're-stamped one fact inside the note',
  retire_entry: 'removed one fact from the note',
}

function operationLabel(row: ProposalHistoryRow): string {
  const op = row.note_edit?.operation || ''
  return OPERATION_WORDS[op] || op
}

/** Whether this decision wrote one list item rather than the whole note. */
function isEntryScope(row: ProposalHistoryRow): boolean {
  return row.note_edit?.scope === 'entry'
}

/** How much of a note a check covered, in words.
 *
 * `partial` is the honest one and the reason this is a sentence rather than the
 * wire value: a re-stamp claims the WHOLE note is still true, so the pass only
 * applies one from complete coverage — "coverage: partial" says none of that to
 * somebody deciding whether to trust the verdict. */
function coverageWord(coverage: string): string {
  if (coverage === 'complete') return 'all'
  if (coverage === 'partial') return 'part'
  return 'an unstated amount'
}

function isChangeOpen(row: ProposalHistoryRow): boolean {
  return openChangeIds.value.has(row.id)
}

async function toggleChange(row: ProposalHistoryRow) {
  const next = new Set(openChangeIds.value)
  if (next.has(row.id)) {
    next.delete(row.id)
    openChangeIds.value = next
    return
  }
  next.add(row.id)
  openChangeIds.value = next
  if (row.change) await store.loadReceipt(row.change.receipt_id, row.workspace)
}

function receiptFor(row: ProposalHistoryRow) {
  return row.change ? store.receipts[store.receiptKey(row.change.receipt_id, row.workspace)] : undefined
}

function receiptError(row: ProposalHistoryRow): string {
  return row.change ? (store.receiptErrors[store.receiptKey(row.change.receipt_id, row.workspace)] ?? '') : ''
}

/** Undo is offered ONLY where a receipt can honour it, and the server checks
 * again: a destination that changed since the operation comes back as a 409,
 * because restoring the before image would delete an unrelated later fact. */
async function undoChange(row: ProposalHistoryRow) {
  if (!row.change?.undoable) return
  await store.undoReceipt(row.change.receipt_id, row.workspace)
}

/** Open the archive this fact came from. Only linked when the server could
 * still find the transcript; otherwise the stem is printed as plain text
 * rather than as a link that goes nowhere. */
async function openSource(row: ProposalHistoryRow) {
  if (!row.source_path) return
  await fileViewer.open(row.source_path)
}

onMounted(() => {
  void store.ensureHistoryLoaded(projectStore.activeWorkspace)
})

// The server scopes the page to one workspace, so a switch needs a new request
// rather than a re-filter of rows that no longer cover the active workspace.
watch(() => projectStore.activeWorkspace, ws => {
  void store.ensureHistoryLoaded(ws)
})

const ACTION_FILTERS: { key: 'all' | 'accepted' | 'dismissed'; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'accepted', label: 'Accepted' },
  { key: 'dismissed', label: 'Dismissed' },
]

const ACTOR_FILTERS: { key: 'all' | 'pwa' | 'agent' | 'auto'; label: string }[] = [
  { key: 'all', label: 'Anyone' },
  { key: 'pwa', label: 'You' },
  { key: 'agent', label: 'Agent' },
  { key: 'auto', label: 'Automatic' },
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
 * label when the row was not a fresh write: already-known facts the accept
 * recognized, and rows an expiry sweep dropped.
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

    <!-- Two segmented controls, the same quiet track as the Review/Map
         switch: sentence case, the current choice lifted rather than a
         pink-outlined pill. -->
    <div class="ph-filters">
      <div class="ph-seg" role="group" aria-label="Decision">
        <button
          v-for="f in ACTION_FILTERS"
          :key="f.key"
          type="button"
          :class="{ active: store.historyActionFilter === f.key }"
          :aria-pressed="store.historyActionFilter === f.key"
          @click="store.historyActionFilter = f.key"
        >{{ f.label }}</button>
      </div>
      <div class="ph-seg" role="group" aria-label="Decided by">
        <button
          v-for="f in ACTOR_FILTERS"
          :key="f.key"
          type="button"
          :class="{ active: store.historyActorFilter === f.key }"
          :aria-pressed="store.historyActorFilter === f.key"
          @click="store.historyActorFilter = f.key"
        >{{ f.label }}</button>
      </div>
      <button
        v-if="filtersActive"
        type="button"
        class="ph-clear-filter"
        @click="store.resetFilters()"
      >Clear filters</button>
    </div>

    <p v-if="store.historyError" class="ph-error" role="alert">{{ store.historyError }}</p>

    <p v-if="store.historyLoading && !store.historyLoaded" class="ph-empty">Loading…</p>
    <!-- A failed fetch shows the error alone. Falling through to the empty
         states printed "No decisions yet." under the error, which claims an
         empty ledger when the ledger merely could not be read. -->
    <template v-else-if="store.historyError && !store.historyLoaded" />
    <!-- Filters only see the decisions fetched so far, so while more pages
         exist "no matches" would be a claim this list cannot make. -->
    <p v-else-if="filtersHideEverything" class="ph-empty">
      No decisions match the current filters<template v-if="store.historyCanLoadMore"> in the
      decisions loaded so far</template>.
    </p>
    <p v-else-if="!filteredRows.length" class="ph-empty">No decisions yet. Suggestions you accept or dismiss show up here.</p>

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
            <!-- A retirement went to the review trash, not through a memory
                 receipt, so the Changes box below says there is no snapshot.
                 Saying so alone would read as "nothing happened" beside a note
                 that really was moved; the trash is where it went and Restore
                 is the way back. -->
            <p v-if="row.reversible_by === 'restore'" class="ph-restore">
              Reversible: this note is in Retired, where <strong>Restore</strong> puts it back.
            </p>
            <p v-if="row.source" class="ph-source-line">
              from
              <button
                v-if="row.source_path"
                type="button"
                class="ph-source-link"
                :title="row.source_path"
                @click="openSource(row)"
              >{{ row.source }}</button>
              <span v-else class="ph-source-name">{{ row.source }}</span>
            </p>

            <!-- A verified note edit, in full: the note it was about, the exact
                 text before and after, the evidence the verdict rested on and
                 how much of the note the check covered. The decision sidecar
                 keeps only the bullet's one line, so without this a settled
                 verification reads as a bare sentence with a destination and
                 nothing a reader could check it against.
                 Collapsed by default for the same reason as Changes: two
                 decisions' worth of note text on an open page is a wall, and
                 nobody scrolls to the bottom of a history to read it. -->
            <details v-if="row.note_edit" class="ph-verify">
              <summary class="ph-verify-summary">
                {{ row.note_edit.pending ? 'The proposal behind this' : 'What was checked' }}
                <span class="ph-verify-op">{{ operationLabel(row) }}</span>
                <span class="ph-verify-outcome">{{ row.note_edit.outcome }}</span>
              </summary>
              <div class="ph-verify-body">
                <p class="ph-verify-path">
                  <code>{{ row.note_edit.relative_path }}</code>
                </p>
                <p class="ph-verify-facts">
                  Covering {{ coverageWord(row.note_edit.coverage) }} of
                  <template v-if="isEntryScope(row)">the fact</template><template v-else>the note</template><template
                    v-if="row.note_edit.evidence.length"
                  >, on {{ row.note_edit.evidence.length }} citation{{ row.note_edit.evidence.length === 1 ? '' : 's' }}</template>.
                </p>
                <p v-if="row.note_edit.reason" class="ph-verify-reason">
                  {{ row.note_edit.reason }}
                </p>
                <ul v-if="row.note_edit.evidence.length" class="ph-verify-evidence">
                  <li v-for="(citation, ci) in row.note_edit.evidence" :key="ci">
                    <span class="ph-verify-source">{{ citation.source_type }} {{ citation.source_ref }}</span>
                    <span class="ph-verify-supports">supports: {{ citation.supports }}</span>
                    <span v-if="citation.quoted" class="ph-verify-quoted">{{ citation.quoted }}</span>
                  </li>
                </ul>

                <!-- The entry, at the entry's own scale. Two whole-note images
                     that differ by one line are unreadable, and on a retirement
                     they differ by a line that is simply gone — so the fact
                     itself is shown, with its before and its after, and the rest
                     of the file is described rather than reprinted. The whole
                     note stays one disclosure away under "Changes", which is
                     where the undo lives. -->
                <template v-if="isEntryScope(row)">
                  <p class="ph-verify-label">The fact, before</p>
                  <pre class="ph-verify-text ph-verify-text--entry">{{ row.note_edit.entry_before || row.note_edit.before }}</pre>
                  <template v-if="row.note_edit.entry_removed">
                    <p class="ph-verify-none">
                      Removed. This one line is gone and every other fact in the
                      note is untouched — and it is reversible: Undo below restores
                      the file exactly as it was.
                    </p>
                  </template>
                  <template v-else-if="row.note_edit.entry_recovery_error">
                    <p class="ph-verify-none">
                      The recorded change could not be read back, so the new text is
                      not shown here rather than guessed at. Changes below still
                      holds the whole note as it was written.
                    </p>
                  </template>
                  <template v-else>
                    <p class="ph-verify-label">The fact, after</p>
                    <pre class="ph-verify-text ph-verify-text--entry">{{ row.note_edit.entry_after }}</pre>
                  </template>
                </template>

                <p v-else-if="row.note_edit.operation !== 'retire'" class="ph-verify-label">Before</p>
                <pre v-if="!isEntryScope(row) && row.note_edit.operation !== 'retire'" class="ph-verify-text">{{ row.note_edit.before }}</pre>
                <p v-if="!isEntryScope(row) && row.note_edit.operation !== 'retire'" class="ph-verify-label">After</p>
                <pre v-if="!isEntryScope(row) && row.note_edit.operation !== 'retire'" class="ph-verify-text">{{ row.note_edit.after }}</pre>
                <p v-if="!isEntryScope(row) && row.note_edit.operation === 'retire'" class="ph-verify-none">
                  A retirement carries no new text: the note was moved, and it is
                  {{ row.reversible_by === 'restore' ? 'in Retired, where Restore puts it back' : 'recoverable from the review trash' }}.
                </p>
                <p v-if="row.note_edit.pending" class="ph-verify-none">
                  Still open — this decision has not been made.
                </p>
              </div>
            </details>

            <!-- Changes. A button rather than <details> because opening it
                 fetches the images, and the expanded state has to drive that. -->
            <button
              type="button"
              class="ph-change-toggle"
              :aria-expanded="isChangeOpen(row)"
              :aria-controls="`ph-change-${row.id}`"
              @click="toggleChange(row)"
            >
              {{ isChangeOpen(row) ? 'Hide changes' : 'Changes' }}
            </button>
            <div v-if="isChangeOpen(row)" :id="`ph-change-${row.id}`" class="ph-change">
              <!-- A conflict is a conflict, never an applied change. A
                   refused accept leaves no receipt, so a row showing one here
                   is either mid-flight or settled; the two are told apart by
                   what the row itself records. -->
              <p v-if="!row.change && row.note_edit?.pending" class="ph-change-none">
                Nothing was written. This decision is still open.
              </p>
              <p v-else-if="!row.change" class="ph-change-none">
                No change snapshot available. This decision was recorded before
                changes were tracked, so what it wrote cannot be shown or undone.
              </p>
              <template v-else>
                <p v-if="store.isReceiptLoading(row.change.receipt_id, row.workspace)" class="ph-change-none" role="status">Loading the change…</p>
                <!-- Above the change, not instead of it. A refused undo is the
                     common case here, and replacing the diff with the refusal
                     left the operator reading "undo would remove unrelated
                     facts" with no sight of the change it was talking about. -->
                <p v-if="receiptError(row)" class="ph-change-error" role="alert">{{ receiptError(row) }}</p>
                <template v-if="!store.isReceiptLoading(row.change.receipt_id, row.workspace) && receiptFor(row)">
                  <p v-if="!receiptFor(row)!.has_snapshot" class="ph-change-none">
                    No change snapshot available.<template v-if="receiptFor(row)!.reason"> {{ receiptFor(row)!.reason }}.</template>
                  </p>
                  <template v-else>
                    <p class="ph-change-dest">{{ receiptFor(row)!.destination || row.destination }}</p>
                    <ul v-if="receiptFor(row)!.diff?.length" class="ph-change-lines">
                      <li
                        v-for="(line, index) in receiptFor(row)!.diff"
                        :key="`${line.op}-${index}`"
                        class="ph-change-line"
                        :class="`ph-change-line--${line.op}`"
                      >
                        <span class="ph-change-sign" aria-hidden="true">{{ line.op === 'added' ? '+' : '−' }}</span>
                        <span class="ph-change-text">{{ line.text }}</span>
                        <span class="ph-sr-only">{{ line.op }}</span>
                      </li>
                    </ul>
                    <p v-else class="ph-change-none">This operation left the destination unchanged.</p>
                    <p v-if="receiptFor(row)!.diff_truncated" class="ph-change-none">Only the first part of the change is shown.</p>
                  </template>
                  <div class="ph-change-actions">
                    <button
                      v-if="receiptFor(row)!.undoable"
                      type="button"
                      class="btn-small btn-chip"
                      :disabled="store.isBusy(row.change.receipt_id)"
                      @click="undoChange(row)"
                    >{{ store.isBusy(row.change.receipt_id) ? 'undoing…' : 'undo this change' }}</button>
                    <span v-else-if="receiptFor(row)!.reason" class="ph-change-none">{{ receiptFor(row)!.reason }}.</span>
                  </div>
                </template>
              </template>
            </div>
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
  color: var(--fg3);
  font-size: var(--text-sm);
  margin: 0;
}

.ph-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

/* Matches MemoryMapView's .mm-seg (the Graph/List switch). */
.ph-seg {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 2px;
  padding: 2px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.ph-seg button {
  min-height: 30px;
  padding: 0 12px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
}
.ph-seg button:hover { color: var(--fg); }
.ph-seg button.active { background: var(--bg3); color: var(--fg); }
@media (pointer: coarse) {
  .ph-seg button { min-height: var(--touch); }
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
  color: var(--fg3);
  font-size: var(--text-sm);
  font-weight: 600;
}

.ph-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--border);
}

/* Hairline rows, like the rest of the page, rather than a bordered card per
   decision. */
.ph-row {
  border-bottom: 1px solid var(--border);
  padding: var(--space-3) 0;
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
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 600;
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

.ph-source-line {
  margin: 0;
  color: var(--fg2);
  font-size: 0.8rem;
}

.ph-source-name {
  font-family: var(--font-mono, ui-monospace, monospace);
  overflow-wrap: anywhere;
}

.ph-restore {
  margin: 0;
  color: var(--fg2);
  font-size: 0.78rem;
}

/* The filed verification behind a `note_edit` decision. Closed by default: the
   full before/after is two notes' worth of text, and a page of 200 decisions
   that rendered it all would be unusable. */
.ph-verify {
  margin-top: 0.2rem;
  align-self: flex-start;
}

.ph-verify-summary {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  color: var(--accent);
  font-size: 0.78rem;
  cursor: pointer;
}

.ph-verify-op {
  color: var(--fg2);
}

.ph-verify-outcome {
  color: var(--fg3);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.72rem;
}

.ph-verify-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm, 6px);
}

.ph-verify-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-1);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg2);
}

.ph-verify-path {
  margin: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.75rem;
  color: var(--fg2);
  overflow-wrap: anywhere;
}

.ph-verify-facts,
.ph-verify-reason {
  margin: 0;
  color: var(--fg2);
  font-size: 0.78rem;
  line-height: 1.5;
}

.ph-verify-evidence {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.ph-verify-evidence li {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-1) var(--space-2);
  border-left: 3px solid var(--border);
  border-radius: 3px;
  background: var(--bg);
  font-size: 0.75rem;
  line-height: 1.5;
}

.ph-verify-source {
  font-family: var(--font-mono, ui-monospace, monospace);
  color: var(--fg2);
  overflow-wrap: anywhere;
}

.ph-verify-supports { color: var(--fg3); }

.ph-verify-quoted {
  color: var(--fg3);
  overflow-wrap: anywhere;
}

.ph-verify-label {
  margin: 0;
  color: var(--fg3);
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

/* The note's own text, exactly as it was filed. Wrapped and scrollable rather
   than clipped: this is the evidence the whole disclosure exists to show, and a
   silently truncated after-image would be the same claim the review card's
   `exact` label refuses to make. */
.ph-verify-text {
  margin: 0;
  max-height: 16em;
  overflow: auto;
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--bg);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.74rem;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* One list item, not a whole file. A whole note's images are for the Changes
   disclosure; here the point is that the reader can see the one line that
   changed without scrolling, so the box is as short as the fact and the fact
   always fits. */
.ph-verify-text--entry {
  max-height: none;
  border-left: 3px solid var(--accent);
}

.ph-verify-none {
  margin: 0;
  color: var(--fg2);
  font-size: 0.78rem;
  line-height: 1.5;
}

/* A link, not a chip: it sits inline in a sentence. The touch rule below is
   the same one .ph-clear-filter uses. */
.ph-source-link {
  background: none;
  border: none;
  padding: 0;
  color: var(--accent);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.8rem;
  cursor: pointer;
  text-decoration: underline;
  overflow-wrap: anywhere;
}

.ph-change-toggle {
  align-self: flex-start;
  margin-top: 0.2rem;
  background: none;
  border: none;
  padding: 0;
  color: var(--accent);
  font-size: 0.78rem;
  cursor: pointer;
}

.ph-change {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-1);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg2);
}

.ph-change-dest {
  margin: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.75rem;
  color: var(--fg2);
}

.ph-change-lines {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* Shape and text first: the sign and the strike-through carry the meaning, so
   the diff reads without colour. */
.ph-change-line {
  display: flex;
  gap: var(--space-2);
  padding: 0.2rem var(--space-2);
  border-left: 3px solid var(--border);
  border-radius: 3px;
  background: var(--bg);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.76rem;
  line-height: 1.5;
}

.ph-change-line--added {
  border-left-color: var(--success);
}

.ph-change-line--removed {
  border-left-color: var(--error);
  text-decoration: line-through;
  color: var(--fg2);
}

.ph-change-sign {
  flex: none;
  opacity: 0.8;
}

.ph-change-text {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.ph-change-none {
  margin: 0;
  color: var(--fg2);
  font-size: 0.78rem;
}

.ph-change-error {
  margin: 0;
  color: var(--error);
  font-size: 0.78rem;
}

.ph-change-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.ph-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
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
  background: none;
  border: none;
  padding: 0;
  color: var(--accent);
  font-size: var(--text-sm);
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

  /* Same trick for the source link, the Changes toggle and the verification
     summary: all glyph-height inline controls, well under the 44px minimum.
     `min-height` rather than padding alone: the summary is an `inline-flex`
     row of three spans, and adding equal padding to each side of it measured
     41px in a real 390px browser — a control the rule is about, under the
     number the rule names. */
  .ph-source-link,
  .ph-change-toggle,
  .ph-verify-summary {
    --ph-hit-visual: 1.1rem;
    --ph-hit-pad: calc((var(--touch, 44px) - var(--ph-hit-visual)) / 2);
    display: inline-block;
    min-height: var(--touch, 44px);
    padding: var(--ph-hit-pad) var(--space-1);
    margin: calc(-1 * var(--ph-hit-pad)) 0;
  }

  /* Undo is a destructive-adjacent action reached from a small row; give it a
     full target rather than the panel's compact chip height. */
  .ph-change-actions .btn-small {
    min-height: var(--touch, 44px);
  }
}
</style>
