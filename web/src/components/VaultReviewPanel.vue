<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useVaultReviewStore } from '../stores/vaultReview'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import type { VaultReviewCandidate, VaultTrashedNote } from '../lib/types'
import { candidateLeaf, signalReasons, verificationLabel } from '../lib/vaultReviewLabels'
import { askConfirm } from '../lib/confirm'

const store = useVaultReviewStore()
const projectStore = useProjectStore()
const fileViewer = useFileViewerStore()

const workspace = computed(() => projectStore.activeWorkspace)

// Failures go to the app's error toast, matching ProposalReviewPanel: an
// inline banner would sit above the list with no way to dismiss it.
watch(
  () => store.error,
  (message) => {
    if (!message) return
    projectStore.pushErrorToast('Retirement action failed', message)
    store.error = ''
  },
)

onMounted(() => {
  if (workspace.value) void store.fetch(workspace.value)
})
watch(workspace, (ws) => {
  if (ws) void store.fetch(ws, { force: true })
})

function refresh() {
  if (workspace.value) void store.fetch(workspace.value, { force: true })
}

// "Later" needs a day count per row. Kept local: it is input state, not
// server state, and it must survive a queue refresh while the user types.
// Stored raw so clearing the field stays empty while typing instead of
// snapping back to 0; only coerced when the Later action runs.
const deferDays = ref<Record<string, string>>({})
function daysFor(id: string): string {
  return deferDays.value[id] ?? '7'
}
function clampDays(id: string): number {
  const raw = (deferDays.value[id] ?? '7').trim()
  if (!raw) return 7
  const n = Math.floor(Number(raw))
  return Number.isFinite(n) ? Math.max(1, Math.min(90, n)) : 7
}

// A candidate carries no content — only its path and why it was flagged —
// so each row lazy-loads an excerpt behind a disclosure, the way the Memory
// Map's detail panel does. Frontmatter is stripped: tags and dates already
// surface as structured rows above the excerpt.
interface ExcerptState {
  loading: boolean
  error: string
  text: string
}
const excerpts = ref<Record<string, ExcerptState>>({})
const EXCERPT_LIMIT = 1200

async function ensureExcerpt(candidate: VaultReviewCandidate) {
  const id = candidate.candidate_id
  if (excerpts.value[id]) return
  excerpts.value[id] = { loading: true, error: '', text: '' }
  try {
    const resp = await fetch(`/api/workspace-file?path=${encodeURIComponent(candidate.path)}`, {
      credentials: 'same-origin',
    })
    if (!resp.ok) {
      excerpts.value[id] = {
        loading: false,
        error: resp.status === 404 ? 'File not found — it may have been moved.' : `Could not load (HTTP ${resp.status}).`,
        text: '',
      }
      return
    }
    let text = await resp.text()
    if (text.startsWith('---')) {
      const end = text.indexOf('\n---', 3)
      if (end !== -1) {
        const after = text.indexOf('\n', end + 4)
        if (after !== -1) text = text.slice(after + 1)
      }
    }
    text = text.trimStart()
    excerpts.value[id] = {
      loading: false,
      error: '',
      text: text.length > EXCERPT_LIMIT ? `${text.slice(0, EXCERPT_LIMIT).trimEnd()} …` : text,
    }
  } catch (e) {
    excerpts.value[id] = {
      loading: false,
      error: e instanceof Error ? e.message : 'Could not load the excerpt.',
      text: '',
    }
  }
}

function onExcerptToggle(candidate: VaultReviewCandidate, event: Event) {
  if ((event.target as HTMLDetailsElement).open) void ensureExcerpt(candidate)
}

async function openNote(path: string) {
  await fileViewer.open(path)
}

function verifyLabelOf(candidate: VaultReviewCandidate): string {
  return verificationLabel(candidate.evidence.age_days, candidate.evidence.last_update)
}

async function keepRow(candidate: VaultReviewCandidate) {
  await store.decide(workspace.value, candidate.candidate_id, 'keep')
}

async function deferRow(candidate: VaultReviewCandidate) {
  await store.decide(workspace.value, candidate.candidate_id, 'defer', clampDays(candidate.candidate_id))
}

async function linkFixedRow(candidate: VaultReviewCandidate) {
  await store.decide(workspace.value, candidate.candidate_id, 'improve_link')
}

async function trashRow(candidate: VaultReviewCandidate) {
  // No confirm here: trash is reversible for 30 days and one click restores
  // it. The confirm budget is spent on permanent deletion instead.
  await store.trash(workspace.value, candidate.candidate_id)
}

async function restoreRow(note: VaultTrashedNote) {
  await store.restore(workspace.value, note.candidate_id)
}

async function deleteRow(note: VaultTrashedNote) {
  const title = note.original_path.split('/').pop() || note.original_path
  if (!await askConfirm(
    `Permanently delete "${title}"? The trash copy is removed and every link to it is rewritten. This cannot be undone.`,
    { title: 'Delete forever', confirmLabel: 'Delete forever', destructive: true },
  )) return
  await store.remove(workspace.value, note.candidate_id)
}

function trashedTitle(note: VaultTrashedNote): string {
  return candidateLeaf(note.original_path)
}

function trashedDate(note: VaultTrashedNote): string {
  if (!note.trashed_at) return ''
  return note.trashed_at.slice(0, 10)
}
</script>

<template>
  <div class="vault-review">
    <header class="vr-head">
      <p class="vr-summary">
        <strong>{{ store.candidates.length }}</strong> to review in {{ workspace }}
        · <strong>{{ store.trashed.length }}</strong> in trash
      </p>
      <button
        type="button"
        class="btn-small btn-chip"
        :disabled="store.loading"
        @click="refresh"
      >{{ store.loading ? 'loading…' : 'refresh' }}</button>
    </header>

    <p class="vr-hint">
      Stale notes the nightly curation flagged. <strong>Still true</strong> re-verifies a
      note; <strong>Retire</strong> moves it to the trash, where one click brings it
      back for 30 days. <strong>Later</strong> snoozes a candidate; <strong>Link
      fixed</strong> records that you re-linked it elsewhere. Nothing here deletes
      permanently except the trash's own delete control, which asks first.
    </p>

    <p v-if="store.loading && !store.candidates.length" class="vr-empty" role="status">Loading candidates…</p>
    <p v-else-if="!store.candidates.length" class="vr-empty">
      Nothing flagged here. Notes land here when curation finds them unlinked,
      duplicated, superseded, or unverified — and leave when you or that run resolves them.
    </p>

    <ul v-else class="vr-rows">
      <li
        v-for="candidate in store.candidates"
        :key="candidate.candidate_id"
        class="vr-row"
        :class="{ 'vr-row--busy': store.isBusy(candidate.candidate_id) }"
      >
        <div class="vr-row-body">
          <div class="vr-row-top">
            <span class="vr-title">{{ candidateLeaf(candidate.path) }}</span>
          </div>
          <button
            type="button"
            class="vr-path"
            :title="candidate.path"
            @click="openNote(candidate.path)"
          >{{ candidate.path }}</button>
          <ul class="vr-reasons">
            <li v-for="reason in signalReasons(candidate.signals)" :key="reason">{{ reason }}</li>
          </ul>
          <p class="vr-meta">
            {{ candidate.evidence.type }} · {{ verifyLabelOf(candidate) }}
            <span v-if="candidate.evidence.backlinks.length">
              · {{ candidate.evidence.backlinks.length }} backlink{{ candidate.evidence.backlinks.length === 1 ? '' : 's' }}
            </span>
            <span v-if="candidate.evidence.bridge" class="vr-badge --warn">bridges clusters — think twice</span>
          </p>
          <p v-if="candidate.evidence.duplicate_group.length" class="vr-meta">
            Possible {{ candidate.evidence.duplicate_group.length === 1 ? 'duplicate' : 'duplicates' }}:
            {{ candidate.evidence.duplicate_group.filter(p => p !== candidate.path).slice(0, 3).join(', ') || 'see evidence' }}
          </p>
          <details class="vr-excerpt" @toggle="onExcerptToggle(candidate, $event)">
            <summary>excerpt</summary>
            <p v-if="excerpts[candidate.candidate_id]?.loading" class="vr-meta">Loading…</p>
            <p v-else-if="excerpts[candidate.candidate_id]?.error" class="vr-error">
              {{ excerpts[candidate.candidate_id].error }}
            </p>
            <pre v-else-if="excerpts[candidate.candidate_id]?.text" class="vr-excerpt-text">{{ excerpts[candidate.candidate_id].text }}</pre>
          </details>
        </div>

        <div class="vr-actions">
          <button
            type="button"
            class="btn-small btn-primary"
            :disabled="store.isBusy(candidate.candidate_id)"
            @click="keepRow(candidate)"
          >{{ store.isBusy(candidate.candidate_id) ? 'working…' : 'Still true' }}</button>
          <button
            type="button"
            class="btn-small btn-chip"
            :disabled="store.isBusy(candidate.candidate_id)"
            @click="trashRow(candidate)"
          >Retire</button>
          <span class="vr-defer">
            <button
              type="button"
              class="btn-small btn-chip"
              :disabled="store.isBusy(candidate.candidate_id)"
              @click="deferRow(candidate)"
            >Later</button>
            <input
              :value="daysFor(candidate.candidate_id)"
              type="number"
              min="1"
              max="90"
              aria-label="Snooze days"
              class="vr-defer-input"
              :disabled="store.isBusy(candidate.candidate_id)"
              @input="deferDays[candidate.candidate_id] = ($event.target as HTMLInputElement).value"
            />
            <span class="vr-defer-unit">days</span>
          </span>
          <button
            type="button"
            class="btn-small btn-chip"
            :disabled="store.isBusy(candidate.candidate_id)"
            title="Record that you re-linked this note elsewhere"
            @click="linkFixedRow(candidate)"
          >Link fixed</button>
        </div>
      </li>
    </ul>

    <section v-if="store.trashed.length" class="vr-trash" aria-label="Trash">
      <h3 class="vr-trash-title">Trash ({{ store.trashed.length }})</h3>
      <p class="vr-hint">Retired notes stay restorable for 30 days. Restore is one click; permanent deletion asks first.</p>
      <ul class="vr-rows">
        <li
          v-for="note in store.trashed"
          :key="note.candidate_id"
          class="vr-row"
          :class="{ 'vr-row--busy': store.isBusy(note.candidate_id) }"
        >
          <div class="vr-row-body">
            <div class="vr-row-top">
              <span class="vr-title">{{ trashedTitle(note) }}</span>
            </div>
            <p class="vr-meta">{{ note.original_path }}<span v-if="trashedDate(note)"> · retired {{ trashedDate(note) }}</span></p>
          </div>
          <div class="vr-actions">
            <button
              type="button"
              class="btn-small btn-primary"
              :disabled="store.isBusy(note.candidate_id)"
              @click="restoreRow(note)"
            >{{ store.isBusy(note.candidate_id) ? 'working…' : 'Restore' }}</button>
            <button
              type="button"
              class="btn-small btn-chip vr-danger"
              :disabled="store.isBusy(note.candidate_id)"
              @click="deleteRow(note)"
            >Delete forever</button>
          </div>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
/* Same one-column rhythm as the proposal queue: generous vertical spacing,
   one shape per row, actions stacked so the text column keeps the width. */
.vault-review {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.vr-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.vr-summary {
  margin: 0;
  font-size: 0.95rem;
}

.vr-hint {
  margin: 0;
  color: var(--fg2);
  font-size: 0.8rem;
  line-height: 1.5;
}

.vr-empty {
  color: var(--fg2);
  font-size: 0.9rem;
  padding: var(--space-4) 0;
}

.vr-error {
  color: var(--error);
  font-size: 0.85rem;
}

.vr-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.vr-row {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: start;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
}

.vr-row--busy {
  opacity: 0.72;
  pointer-events: none;
}

.vr-row-body {
  min-width: 0;
}

.vr-row-top {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
}

.vr-title {
  font-size: 0.95rem;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.vr-path {
  background: none;
  border: none;
  padding: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.72rem;
  color: var(--accent);
  text-align: left;
  cursor: pointer;
  overflow-wrap: anywhere;
  min-height: var(--touch);
}

.vr-path:hover {
  text-decoration: underline;
}

.vr-reasons {
  margin: var(--space-1) 0 0;
  padding-left: 1.1rem;
  color: var(--fg);
  font-size: 0.82rem;
  line-height: 1.5;
}

.vr-meta {
  margin: 0.25rem 0 0;
  color: var(--fg2);
  font-size: 0.8rem;
}

.vr-badge {
  margin-left: var(--space-2);
  font-size: 0.7rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
}

.vr-badge.--warn {
  background: rgba(210, 153, 34, 0.18);
  color: var(--warning);
}

.vr-excerpt {
  margin-top: var(--space-2);
  font-size: 0.8rem;
  color: var(--fg2);
}

.vr-excerpt summary {
  cursor: pointer;
  min-height: var(--touch);
  display: inline-flex;
  align-items: center;
}

.vr-excerpt-text {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  max-height: 12rem;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.75rem;
  line-height: 1.5;
  color: var(--fg2);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.vr-actions {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-1);
  flex: none;
  min-width: 8.5rem;
}

.vr-defer {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

.vr-defer-input {
  width: 3.5rem;
  min-height: var(--touch);
}

.vr-defer-unit {
  font-size: 0.75rem;
  color: var(--fg2);
}

/* Destructive, but not competing-pink: neutral chip in the error colour. */
.vr-danger {
  color: var(--error);
  border-color: var(--error);
}

.vr-trash {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
}

.vr-trash-title {
  margin: 0;
  font-size: 0.9rem;
}

/* Stacked column keeps text full-width on both desktop and mobile. */
@media (max-width: 640px) {
  .vr-row {
    grid-template-columns: 1fr;
  }
}
</style>
