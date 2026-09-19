<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useVaultReviewStore } from '../stores/vaultReview'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import type { VaultReviewCandidate, VaultTrashedNote,
  VaultClearedNote } from '../lib/types'
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
//
// `immediate: true` because this panel is a `v-if` sibling that is unmounted
// on every tab flip: `store.decide` awaits a POST, so switching to Proposals
// (or off the page) while it is in flight stops this watcher before the notice
// is set. Registered fresh on remount against an already-non-empty ref, a
// lazy watcher never fires and the message is lost — the exact silent
// half-success the notice exists to report. The immediate run drains it.
watch(
  () => store.notice,
  (message) => {
    if (!message) return
    projectStore.pushToast({ chat_id: '', title: 'Nothing to stamp', body: message, variant: 'info' })
    store.notice = ''
  },
  { immediate: true },
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
    discussTask(candidate)
  )
}

/** What to ask the agent for, which the `unlinked` signal changes.
 *
 * An unlinked note is the one case where the queue's own question — keep or
 * retire — is not the most useful one to open with. Nothing links to it, and
 * the repair for that is a link, which by definition lives in some *other*
 * note. So the seed sends the agent hunting for the notes that should point
 * here, which is work that actually clears the signal: backlinks are counted
 * live, so a note that gains one stops being flagged `unlinked` on the next
 * run. The retired `Link fixed` button claimed exactly this repair and
 * performed none of it — it cleared the row and left the note as orphaned as
 * it found it.
 *
 * Nothing is written unattended either way. The read-only branch stays
 * read-only, and the unlinked branch narrows the permission rather than
 * dropping it: links into other notes, once approved, and no edit to the note
 * under review.
 */
function discussTask(candidate: VaultReviewCandidate): string {
  if (!candidate.signals.includes('unlinked')) {
    return (
      'Read the note and tell me what would be lost if it went, and whether ' +
      'anything in it belongs somewhere else first. Do not edit, move, or delete ' +
      'anything — I will pick Still true or Retire myself.'
    )
  }
  return (
    // The approval gate leads, as the read-only branch's constraint does.
    // Buried mid-paragraph it was one clause among five, and the cost of a
    // model missing it is a write the user never approved — which, because
    // backlinks are counted live, also clears the row on its own.
    'Write nothing until I say so. Read the note, then search the vault for ' +
    'the notes that should link to it — the projects, people, or decisions it ' +
    'belongs to. Propose each one as the note the link would go in and the ' +
    'sentence it would read, and wait for my go-ahead on each before writing ' +
    'it: a link is what actually clears this flag, since a note that gains a ' +
    'backlink stops being flagged for it. If nothing should link to it, say so ' +
    'plainly — that is an argument for retiring it. Do not edit, move, or ' +
    'delete the note itself; I will pick Still true or Retire myself.'
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
      // The chat's name follows the question the seed actually opens with:
      // "Retire Mo?" over a chat hunting for links to Mo is the same small
      // dishonesty the `Link fixed` button was removed for.
      title: candidate.signals.includes('unlinked')
        ? `Link or retire ${candidateLeaf(candidate.path)}?`
        : `Retire ${candidateLeaf(candidate.path)}?`,
      seed: discussPrompt(candidate),
    })
  } finally {
    chatBusy.value = false
  }
}

async function keepRow(candidate: VaultReviewCandidate) {
  await store.decide(workspace.value, candidate.candidate_id, 'keep')
}

async function trashRow(candidate: VaultReviewCandidate) {
  // No confirm here: trash is reversible and one click restores it. The
  // confirm budget is spent on permanent deletion instead.
  await store.trash(workspace.value, candidate.candidate_id)
}

async function reopenRow(note: VaultClearedNote) {
  await store.reopen(workspace.value, note.candidate_id)
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

/** The date half of an ISO timestamp, or '' when there is none. One helper for
 * both rows: the retired and the cleared lists print the same shape off
 * different field names, and two copies of the rule could drift apart. */
function isoDate(stamp: string | undefined): string {
  if (!stamp) return ''
  return stamp.slice(0, 10)
}

function trashedDate(note: VaultTrashedNote): string {
  return isoDate(note.trashed_at)
}

function clearedDate(note: VaultClearedNote): string {
  return isoDate(note.decided_at)
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
          <strong>{{ store.candidates.length }}</strong> to revisit in {{ workspace }}
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
      <!-- One sentence, then the mechanism folded away. The paragraph this
           replaces spent eight lines on what each button does before the first
           note appeared; the buttons are on every row and say so themselves. -->
      <p class="vr-lede">
        Notes you already saved that may have gone out of date. Say whether each one
        still holds.
      </p>
      <details class="vr-how">
        <summary class="vr-how-summary">What each choice does</summary>
        <div class="vr-how-body">
          <p>
            <strong>Still true</strong> takes the note off this list and marks it
            checked today — though a note with no frontmatter has nowhere to record
            that, and Ciaobot will say so when that happens.
          </p>
          <p>
            <strong>Retire</strong> moves the note to <strong>Retired</strong>, where
            one click brings it back. Nothing is deleted for good except through the
            delete control there, which asks first.
          </p>
          <p>
            <strong>Talk about it</strong> opens a chat with the note pinned and
            decides nothing. When nothing links to a note, that chat goes looking for
            the notes that should link to it — which is what actually clears the flag.
          </p>
          <p>Leaving a row alone keeps it here.</p>
        </div>
      </details>

      <p v-if="store.loading && !store.candidates.length" class="vr-empty" role="status">Loading candidates…</p>
      <p v-else-if="!store.candidates.length" class="vr-empty">
        Nothing to revisit. A note turns up here when it has gone a long time
        unchecked, nothing links to it, or it looks like a duplicate of another note.
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
              :disabled="chatBusy"
              title="Open a chat about this note before deciding"
              @click="discussRow(candidate)"
            >Talk about it</button>
          </div>
        </li>
      </ul>

      <!-- The way back from a "Still true". A keep is suppressed by content
           hash, so short of editing the note there was no route from clearing
           a row by mistake to getting it back — and the note `keep` cannot
           stamp, the one with no frontmatter, is the likeliest mistake. Closed
           by default: it is a correction, not part of the pass. -->
      <details v-if="store.cleared.length" class="vr-cleared">
        <summary class="vr-cleared-summary">
          Recently cleared ({{ store.cleared.length }})
        </summary>
        <p class="vr-hint vr-cleared-hint">
          Notes you marked <strong>Still true</strong>. They stay out of the queue until
          the note changes; <strong>Add back</strong> returns one now.
        </p>
        <ul class="vr-rows">
          <li
            v-for="note in store.cleared"
            :key="note.candidate_id"
            class="vr-row"
            :class="{ 'vr-row--busy': store.isBusy(note.candidate_id) }"
          >
            <div class="vr-row-body">
              <div class="vr-row-top">
                <span class="vr-title">{{ candidateLeaf(note.path) }}</span>
              </div>
              <p class="vr-meta">{{ note.path }}<span v-if="clearedDate(note)"> · cleared {{ clearedDate(note) }}</span></p>
            </div>
            <div class="vr-actions">
              <button
                type="button"
                class="btn-small btn-chip"
                :disabled="store.isBusy(note.candidate_id)"
                title="Put this note back in the review queue"
                @click="reopenRow(note)"
              >{{ store.isBusy(note.candidate_id) ? 'working…' : 'Add back' }}</button>
            </div>
          </li>
        </ul>
      </details>
    </template>

    <section v-else class="vr-trash" aria-label="Trash">
      <p class="vr-lede">
        Notes you retired. They stay here until you say otherwise — nothing is removed
        on a timer. Restore is one click; deleting for good asks first.
      </p>
      <p v-if="!store.trashed.length" class="vr-empty">
        Nothing retired yet. A note you retire from <strong>Notes to revisit</strong>
        waits here until you restore it or delete it for good.
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

/* The one sentence that says what this list is — full contrast, body size,
   because it is the first thing read. */
.vr-lede {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.5;
  max-width: 62ch;
}

/* The per-button detail, folded away: closed it costs one line, and the
   summary is a real disclosure control, so it is keyboard-reachable. */
.vr-how {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
}

.vr-how-summary {
  display: inline-flex;
  align-items: center;
  min-height: var(--touch);
  color: var(--fg2);
  cursor: pointer;
}

.vr-how-summary:hover { color: var(--fg); }
.vr-how-summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.vr-how-body {
  max-width: 62ch;
  line-height: 1.5;
}

.vr-how-body p {
  margin: 0 0 var(--space-2);
}

.vr-how-body p:last-child { margin-bottom: 0; }

.vr-empty {
  color: var(--fg2);
  font-size: 0.9rem;
  padding: var(--space-4) 0;
}

/* A correction, not part of the pass: quieter than the queue above it, and
   separated by a rule so the two lists never read as one. */
.vr-cleared {
  margin-top: var(--space-4);
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}

.vr-cleared-summary {
  cursor: pointer;
  color: var(--fg2);
  font-size: 0.85rem;
  padding: var(--space-1) 0;
}

.vr-cleared-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

.vr-cleared-hint {
  margin: var(--space-2) 0 var(--space-3);
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
