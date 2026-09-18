<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useVaultReviewStore } from '../stores/vaultReview'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import type { VaultReviewCandidate, VaultTrashedNote } from '../lib/types'
import { candidateLeaf, signalReasons, verificationLabel } from '../lib/vaultReviewLabels'
import { askConfirm } from '../lib/confirm'
import { parseFrontmatter } from '../lib/markdownFrontmatter'
import { startFileDiscussion } from '../lib/fileDiscussion'

/** Which half of the retirement queue to render.
 *
 * Deciding about a note and emptying the trash are different jobs on different
 * rows, and they used to share one scroll: a full queue put thirty candidates
 * between a note you had just retired and the Restore button that brings it
 * back. The parent owns the tab state; both sections stay in this component
 * because they share the store, the busy set and the error toast.
 */
const props = withDefaults(defineProps<{ section?: 'candidates' | 'trash' }>(), {
  section: 'candidates',
})

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

// An action that succeeded but did not do everything its label promises. Not
// an error — the row really is cleared — so it takes the auto-dismissing info
// variant rather than the persistent error toast.
watch(
  () => store.notice,
  (message) => {
    if (!message) return
    projectStore.pushToast({ chat_id: '', title: 'Nothing to stamp', body: message, variant: 'info' })
    store.notice = ''
  },
)

onMounted(() => {
  // `ensureLoaded`, not `fetch`: the panel is a `v-if` sibling of the
  // proposals panel, so every tab flip remounts it and a plain fetch would
  // rescan the whole vault each time.
  if (workspace.value) void store.ensureLoaded(workspace.value)
})
watch(workspace, (ws) => {
  if (ws) void store.fetch(ws, { force: true })
})

function refresh() {
  if (workspace.value) void store.fetch(workspace.value, { force: true })
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
// The codes `/api/workspace-file` actually returns (404 missing, 413 over the
// size cap, 415 not an allowlisted extension); anything else falls through to
// the raw status. Deliberately no 403: the endpoint never sends one, and the
// only 403 in the app is the auth middleware's "forbidden origin", which is
// not a fact about this file.
const EXCERPT_ERRORS: Record<number, string> = {
  404: 'File not found — it may have been moved.',
  413: 'File too large to preview.',
  415: 'Cannot preview this file type.',
}

async function ensureExcerpt(candidate: VaultReviewCandidate) {
  const id = candidate.candidate_id
  // A cached success (or an in-flight load) is reused; a cached FAILURE is
  // not, or a single transient error would pin "Could not load" on the row
  // for the life of the panel with no way to retry but a full refresh.
  const cached = excerpts.value[id]
  if (cached && !cached.error) return
  excerpts.value[id] = { loading: true, error: '', text: '' }
  try {
    const resp = await fetch(`/api/workspace-file?path=${encodeURIComponent(candidate.path)}`, {
      credentials: 'same-origin',
    })
    if (!resp.ok) {
      excerpts.value[id] = {
        loading: false,
        error: EXCERPT_ERRORS[resp.status] || `Could not load (HTTP ${resp.status}).`,
        text: '',
      }
      return
    }
    // Shared splitter, not a local `startsWith('---')` scan: it handles a BOM,
    // CRLF line endings and a missing closing fence, which a hand-rolled copy
    // silently renders as raw YAML.
    const text = parseFrontmatter(await resp.text()).body.trimStart()
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

/** The opening lines the queue already sent with the row.
 *
 * Every row used to start as a path and four bullet points with the note's own
 * words hidden behind a disclosure, so telling two stale project logs apart
 * meant opening both. The server now carries a short excerpt in the candidate's
 * evidence; the disclosure below still loads the longer text on demand, and an
 * older server that sends no excerpt simply falls back to it.
 */
function inlineExcerpt(candidate: VaultReviewCandidate): string {
  return candidate.evidence.excerpt?.trim() || ''
}

async function openNote(path: string) {
  await fileViewer.open(path)
}

function verifyLabelOf(candidate: VaultReviewCandidate): string {
  return verificationLabel(candidate.evidence.age_days, candidate.evidence.last_update)
}

// One chat at a time: the button is on every row, and a double-click used to
// be able to open two chats about the same note before the first returned.
const chatBusy = ref(false)

/** What the queue knows about a candidate, as a sentence the agent can read.
 *
 * The row already shows all of it; repeating it in the message means the chat
 * starts from the same evidence the decision is being made on, without the
 * agent having to re-derive why curation flagged the note.
 */
function discussPrompt(candidate: VaultReviewCandidate): string {
  const reasons = signalReasons(candidate.signals)
  const facts = [candidate.evidence.type, verifyLabelOf(candidate)]
  const backlinks = candidate.evidence.backlinks.length
  facts.push(backlinks ? `${backlinks} backlink${backlinks === 1 ? '' : 's'}` : 'no backlinks')
  if (candidate.evidence.bridge) facts.push('it bridges two clusters')
  const duplicates = candidate.evidence.duplicate_group.filter(p => p !== candidate.path)
  if (duplicates.length) facts.push(`possible duplicates: ${duplicates.slice(0, 3).join(', ')}`)
  return (
    `I am deciding whether to retire \`${candidate.path}\` from the ` +
    `${candidate.workspace} vault.\n\n` +
    `Curation flagged it because ${reasons.join('; ') || 'it looked stale'}. ` +
    `It is ${facts.join(' · ')}.\n\n` +
    'Read the note and tell me what would be lost if it went, and whether ' +
    'anything in it belongs somewhere else first. Do not edit, move, or delete ' +
    'anything — I will pick Still true, Retire, or Link fixed myself.'
  )
}

/** Open a chat about this candidate, with the note pinned beside it.
 *
 * The candidate stays queued and nothing is sent: the message lands in the
 * composer as a draft so the specific question — "what links to this?", "is
 * this the same person as X?" — can replace it before it goes anywhere.
 */
async function discussRow(candidate: VaultReviewCandidate) {
  if (chatBusy.value) return
  chatBusy.value = true
  try {
    await startFileDiscussion(projectStore, {
      path: candidate.path,
      workspace: candidate.workspace,
      title: `Retire ${candidateLeaf(candidate.path)}?`,
      seed: discussPrompt(candidate),
    })
  } finally {
    chatBusy.value = false
  }
}

async function keepRow(candidate: VaultReviewCandidate) {
  await store.decide(workspace.value, candidate.candidate_id, 'keep')
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
  const title = candidateLeaf(note.original_path)
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
        <template v-if="props.section === 'trash'">
          <strong>{{ store.trashed.length }}</strong> retired {{ store.trashed.length === 1 ? 'note' : 'notes' }} in {{ workspace }}
        </template>
        <template v-else>
          <strong>{{ store.candidates.length }}</strong> to review in {{ workspace }}
        </template>
      </p>
      <button
        type="button"
        class="btn-small btn-chip"
        :disabled="store.loading"
        @click="refresh"
      >{{ store.loading ? 'loading…' : 'refresh' }}</button>
    </header>

    <template v-if="props.section !== 'trash'">
      <p class="vr-hint">
        Stale notes the nightly curation flagged. <strong>Still true</strong> clears the
        row and stamps the note as verified today (a note with no frontmatter has
        nothing to stamp, and says so); <strong>Retire</strong> moves it to the
        Trash tab, where one click brings it back for 30 days. <strong>Link fixed</strong>
        clears it because you have since linked it from somewhere else, and changes nothing
        in the note. Leaving a row alone keeps it here. Nothing here deletes permanently
        except the trash's own delete control, which asks first.
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
            <p v-if="inlineExcerpt(candidate)" class="vr-excerpt-inline">{{ inlineExcerpt(candidate) }}</p>
            <details class="vr-excerpt" @toggle="onExcerptToggle(candidate, $event)">
              <summary>{{ inlineExcerpt(candidate) ? 'read more' : 'excerpt' }}</summary>
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
              title="Clear the row, and stamp the note's updated date as today when it has frontmatter to stamp"
              @click="keepRow(candidate)"
            >{{ store.isBusy(candidate.candidate_id) ? 'working…' : 'Still true' }}</button>
            <button
              type="button"
              class="btn-small btn-chip"
              :disabled="store.isBusy(candidate.candidate_id)"
              @click="trashRow(candidate)"
            >Retire</button>
            <button
              type="button"
              class="btn-small btn-chip"
              :disabled="store.isBusy(candidate.candidate_id)"
              title="Record that you re-linked this note elsewhere; the note is not edited"
              @click="linkFixedRow(candidate)"
            >Link fixed</button>
            <button
              type="button"
              class="btn-small btn-chip"
              :disabled="chatBusy"
              title="Open a chat about this note before deciding"
              @click="discussRow(candidate)"
            >Talk about it</button>
          </div>
        </li>
      </ul>
    </template>

    <section v-else class="vr-trash" aria-label="Trash">
      <p class="vr-hint">Retired notes stay restorable for 30 days. Restore is one click; permanent deletion asks first.</p>
      <p v-if="!store.trashed.length" class="vr-empty">
        Nothing retired yet. A note you retire from the To review tab waits here for
        30 days before it can be deleted for good.
      </p>
      <ul v-else class="vr-rows">
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

/* The note's own words, visible without a click. Clamped rather than truncated
   server-side alone: the excerpt is a sentence or two, and three lines is as
   much as a row can give it without the actions drifting out of reach. */
.vr-excerpt-inline {
  margin: var(--space-2) 0 0;
  font-size: 0.82rem;
  line-height: 1.5;
  color: var(--fg2);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  overflow-wrap: anywhere;
}

.vr-excerpt {
  margin-top: var(--space-1);
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

/* Destructive, but not competing-pink: neutral chip in the error colour. */
.vr-danger {
  color: var(--error);
  border-color: var(--error);
}

/* Its own tab now, so no separator rule and no heading: the tab above says
   what this is, and the border only made sense when the trash was pinned
   under the candidate list in the same scroll. */
.vr-trash {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* Stacked column keeps text full-width on both desktop and mobile. */
@media (max-width: 640px) {
  .vr-row {
    grid-template-columns: 1fr;
  }
}
</style>
