<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useVaultReviewStore } from '../stores/vaultReview'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import type { VaultReviewCandidate, VaultTrashedNote,
  VaultClearedNote } from '../lib/types'
import {
  candidateLeaf, orderedSignals, signalChipLabel, signalLabel, signalReasons, signalRowLabel, verificationLabel,
} from '../lib/vaultReviewLabels'
import { askConfirm } from '../lib/confirm'
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
const hasCurrentSnapshot = computed(() =>
  Boolean(workspace.value) && store.loadedWorkspace === workspace.value,
)
const visibleCandidates = computed(() => hasCurrentSnapshot.value ? store.candidates : [])
const visibleTrashed = computed(() => hasCurrentSnapshot.value ? store.trashed : [])
const visibleCleared = computed(() => hasCurrentSnapshot.value ? store.cleared : [])
const showInitialLoading = computed(() => store.loading && !hasCurrentSnapshot.value)
const showInitialError = computed(() => !store.loading && !hasCurrentSnapshot.value && Boolean(store.loadError))
const showStaleError = computed(() => hasCurrentSnapshot.value && Boolean(store.loadError))

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

// -- Reason filter ----------------------------------------------------------
//
// One chip per reason actually present, with its count. A note flagged for two
// reasons counts under both. Local to the panel: the section remounts on every
// switch, and coming back to the full list is the right default.
const signalFilter = ref('all')

const signalCounts = computed(() => {
  const tally = new Map<string, number>()
  for (const c of visibleCandidates.value) {
    for (const s of new Set(c.signals)) tally.set(s, (tally.get(s) ?? 0) + 1)
  }
  return orderedSignals([...tally.keys()])
    .map(signal => ({ signal, label: signalChipLabel(signal), count: tally.get(signal) ?? 0 }))
    .filter(chip => chip.count > 0)
})

const shownCandidates = computed(() => signalFilter.value === 'all'
  ? visibleCandidates.value
  : visibleCandidates.value.filter(c => c.signals.includes(signalFilter.value)))

// A chip whose last row was just decided disappears; fall back to All rather
// than leave a filter selected that no chip shows.
watch(signalCounts, (chips) => {
  if (signalFilter.value !== 'all' && !chips.some(c => c.signal === signalFilter.value)) {
    signalFilter.value = 'all'
  }
})

// -- Evidence ----------------------------------------------------------------
//
// Each reason on a row is a disclosure button: it opens the evidence the queue
// holds for that reason — the quoted line for "superseded", the date and where
// it came from for "unchecked" — directly under the reason line.
const openEvidence = ref<Set<string>>(new Set())

function evidenceKey(candidate: VaultReviewCandidate, signal: string): string {
  return `${candidate.candidate_id}:${signal}`
}

function evidenceId(candidate: VaultReviewCandidate, signal: string): string {
  return `vr-ev-${candidate.candidate_id}-${signal}`.replace(/[^A-Za-z0-9_-]/g, '-')
}

function isEvidenceOpen(candidate: VaultReviewCandidate, signal: string): boolean {
  return openEvidence.value.has(evidenceKey(candidate, signal))
}

function toggleEvidence(candidate: VaultReviewCandidate, signal: string) {
  const key = evidenceKey(candidate, signal)
  const next = new Set(openEvidence.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  openEvidence.value = next
}

function rowSignals(candidate: VaultReviewCandidate): string[] {
  return orderedSignals(candidate.signals)
}

function backlinkLabel(candidate: VaultReviewCandidate): string {
  const n = candidate.evidence.backlinks.length
  if (!n) return 'no backlinks'
  return `${n} backlink${n === 1 ? '' : 's'}`
}

/** "Person notes", "Project notes": the type the age limit belongs to. */
function typePlural(candidate: VaultReviewCandidate): string {
  const type = (candidate.evidence.type || 'note').trim()
  const word = type.charAt(0).toUpperCase() + type.slice(1)
  return /notes?$/i.test(word) ? word.replace(/notes?$/i, 'notes') : `${word} notes`
}

interface QuotedLine { line: number; parts: Array<{ text: string; mark: boolean }>; hit: boolean }

/** The superseded line with a line either side, the matched phrase split out
 * so it renders in a <mark> without v-html. */
function quotedLines(candidate: VaultReviewCandidate): QuotedLine[] {
  const ev = candidate.evidence.superseded
  if (!ev) return []
  const out: QuotedLine[] = []
  if (ev.before) out.push({ line: ev.before.line, parts: [{ text: ev.before.text, mark: false }], hit: false })
  out.push({ line: ev.line, parts: markParts(ev.text, ev.match), hit: true })
  if (ev.after) out.push({ line: ev.after.line, parts: [{ text: ev.after.text, mark: false }], hit: false })
  return out
}

function markParts(text: string, match: string): Array<{ text: string; mark: boolean }> {
  const at = match ? text.toLowerCase().indexOf(match.toLowerCase()) : -1
  if (at < 0) return [{ text, mark: false }]
  return [
    { text: text.slice(0, at), mark: false },
    { text: text.slice(at, at + match.length), mark: true },
    { text: text.slice(at + match.length), mark: false },
  ].filter(p => p.text)
}

function duplicatesOf(candidate: VaultReviewCandidate): string[] {
  return candidate.evidence.duplicate_group.filter(p => p !== candidate.path)
}

async function openAtLine(path: string, line: number) {
  await fileViewer.open(path, line)
}

/** The opening lines the queue sent with the row, clamped to two lines. */
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
  if (candidate.evidence.bridge) facts.push('many notes link to or from it')
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
      <h2 class="mr-head vr-summary">
        <template v-if="props.section === 'trash'">
          {{ visibleTrashed.length }} retired {{ visibleTrashed.length === 1 ? 'note' : 'notes' }}
        </template>
        <template v-else>
          {{ visibleCandidates.length }} to revisit
        </template>
      </h2>
      <!-- One sentence says what the two buttons do; the rows repeat nothing. -->
      <p v-if="props.section !== 'trash'" class="mr-lede vr-lede">
        Saved notes that may have gone out of date. <strong>Still true</strong> marks a note
        checked today; <strong>Retire</strong> moves it to Retired, where it can be restored.
      </p>
      <p v-else class="mr-lede vr-lede">
        Notes you retired. They stay here until you say otherwise — nothing is removed
        on a timer. Restore is one click; deleting for good asks first.
      </p>
    </header>

    <div v-if="showStaleError" class="vr-load-state vr-load-state--stale" role="status">
      <span>Could not refresh this list. Showing the last successful load.</span>
      <button
        type="button"
        class="btn-small"
        :disabled="store.loading"
        @click="refresh"
      >{{ store.loading ? 'Retrying…' : 'Retry' }}</button>
    </div>

    <template v-if="props.section !== 'trash'">
      <p v-if="showInitialLoading" class="vr-empty" role="status">Loading candidates…</p>
      <div v-else-if="showInitialError" class="vr-load-state vr-load-state--error" role="alert">
        <span>Could not load notes to revisit. {{ store.loadError }}</span>
        <button type="button" class="btn-small" @click="refresh">Retry</button>
      </div>
      <p v-else-if="hasCurrentSnapshot && !visibleCandidates.length" class="vr-empty">
        Nothing to revisit. A note turns up here when it has gone a long time
        unchecked, nothing links to it, or it looks like a duplicate of another note.
      </p>

      <template v-else-if="visibleCandidates.length">
        <div class="mr-chips vr-chips" role="group" aria-label="Why notes are here">
          <button
            type="button"
            class="mr-chip"
            :aria-pressed="signalFilter === 'all'"
            @click="signalFilter = 'all'"
          >All <span class="mr-chip-count">{{ visibleCandidates.length }}</span></button>
          <button
            v-for="chip in signalCounts"
            :key="chip.signal"
            type="button"
            class="mr-chip"
            :aria-pressed="signalFilter === chip.signal"
            @click="signalFilter = chip.signal"
          >{{ chip.label }} <span class="mr-chip-count">{{ chip.count }}</span></button>
        </div>

        <ul class="vr-rows">
          <li
            v-for="candidate in shownCandidates"
            :key="candidate.candidate_id"
            class="vr-row"
            :class="{ 'vr-row--busy': store.isBusy(candidate.candidate_id) }"
          >
            <div class="vr-row-body">
              <h3 class="vr-title">{{ candidateLeaf(candidate.path) }}</h3>
              <!-- type · reason(s) · backlinks. Each reason is the disclosure
                   for its own evidence. -->
              <p class="vr-why">
                <span>{{ candidate.evidence.type }}</span>
                <template v-for="signal in rowSignals(candidate)" :key="signal">
                  <span class="vr-why-sep" aria-hidden="true">·</span>
                  <button
                    type="button"
                    class="mr-flag vr-flag"
                    :aria-expanded="isEvidenceOpen(candidate, signal)"
                    :aria-controls="evidenceId(candidate, signal)"
                    @click="toggleEvidence(candidate, signal)"
                  >{{ signalRowLabel(signal, candidate.evidence) }}<svg class="vr-flag-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg></button>
                </template>
                <span class="vr-why-sep" aria-hidden="true">·</span>
                <span>{{ backlinkLabel(candidate) }}</span>
                <span v-if="candidate.evidence.bridge" class="vr-badge --warn">well linked — think twice</span>
              </p>

              <template v-for="signal in rowSignals(candidate)" :key="`ev-${signal}`">
                <div
                  v-show="isEvidenceOpen(candidate, signal)"
                  :id="evidenceId(candidate, signal)"
                  class="mr-box vr-evidence"
                  :class="`vr-evidence--${signal}`"
                >
                  <template v-if="signal === 'superseded_language' && candidate.evidence.superseded">
                    <div class="mr-box-head">
                      <span>Where it says so · {{ candidate.evidence.superseded.where === 'frontmatter' ? 'frontmatter' : 'lead paragraph' }}, line {{ candidate.evidence.superseded.line }}</span>
                      <button
                        type="button"
                        class="mr-link"
                        @click="openAtLine(candidate.path, candidate.evidence.superseded.line)"
                      >Open at line {{ candidate.evidence.superseded.line }}</button>
                    </div>
                    <ol class="mr-box-lines">
                      <li
                        v-for="q in quotedLines(candidate)"
                        :key="q.line"
                        class="mr-box-line mr-box-line--nosign"
                        :class="{ 'mr-box-line--hit': q.hit }"
                      >
                        <span class="mr-box-num">{{ q.line }}</span>
                        <span class="mr-box-text"><template v-for="(part, pi) in q.parts" :key="pi"><mark v-if="part.mark">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
                      </li>
                    </ol>
                    <p class="mr-box-foot">
                      Only the frontmatter and the opening paragraph count. If this sentence is
                      about part of the note, not all of it, <strong>Still true</strong> keeps it.
                    </p>
                  </template>
                  <p v-else-if="signal === 'superseded_language'" class="mr-box-body">
                    Its frontmatter or opening paragraph says it was superseded.
                  </p>

                  <p v-else-if="signal === 'unverified'" class="mr-box-body">
                    <template v-if="candidate.evidence.unverified?.source === 'mtime'">
                      No <code>updated:</code> field, so the file's modified date is used:
                      <code>{{ candidate.evidence.unverified.last_verified }}</code>.
                    </template>
                    <template v-else-if="candidate.evidence.unverified">
                      Last checked <code>{{ candidate.evidence.unverified.last_verified }}</code>,
                      from the note's <code>updated:</code> field.
                    </template>
                    <template v-else>{{ verifyLabelOf(candidate) }}.</template>
                    <template v-if="candidate.evidence.unverified">
                      {{ typePlural(candidate) }} are due every {{ candidate.evidence.unverified.threshold_days }} days.
                    </template>
                  </p>

                  <p v-else-if="signal === 'possible_duplicate'" class="mr-box-body">
                    <template v-if="duplicatesOf(candidate).length">
                      Looks like
                      <template v-for="(dup, di) in duplicatesOf(candidate).slice(0, 3)" :key="dup"><template v-if="di">, </template><code>{{ dup }}</code></template>.
                    </template>
                    <template v-else>It may say the same thing as another note.</template>
                  </p>

                  <p v-else-if="signal === 'unlinked'" class="mr-box-body">
                    No other note links to it<template v-if="candidate.evidence.outbound_links.length">, though it links out to {{ candidate.evidence.outbound_links.length }}</template>.
                    A link from the note it belongs to clears this.
                  </p>

                  <p v-else-if="signal === 'weak_provenance'" class="mr-box-body">
                    Its frontmatter has no <code>updated:</code> date, tags or aliases, so nothing
                    says when it was written or what it is about.
                  </p>

                  <p v-else class="mr-box-body">Flagged because {{ signalLabel(signal) }}.</p>
                </div>
              </template>

              <p v-if="inlineExcerpt(candidate)" class="vr-excerpt-inline">{{ inlineExcerpt(candidate) }}</p>
              <button
                type="button"
                class="vr-path"
                :title="`Open ${candidate.path}`"
                @click="openNote(candidate.path)"
              >{{ candidate.path }}</button>
            </div>

            <div class="mr-actions vr-actions">
              <button
                type="button"
                class="mr-btn"
                :disabled="store.isBusy(candidate.candidate_id)"
                title="Clear the row, and stamp the note's updated date as today when it has frontmatter to stamp"
                @click="keepRow(candidate)"
              >{{ store.isBusy(candidate.candidate_id) ? 'working…' : 'Still true' }}</button>
              <button
                type="button"
                class="mr-btn mr-btn--quiet"
                :disabled="store.isBusy(candidate.candidate_id)"
                @click="trashRow(candidate)"
              >Retire</button>
              <button
                type="button"
                class="mr-link"
                :disabled="chatBusy"
                title="Open a chat about this note before deciding"
                @click="discussRow(candidate)"
              >Discuss</button>
            </div>
          </li>
        </ul>
      </template>

      <!-- The way back from a "Still true". A keep is suppressed by content
           hash, so short of editing the note there was no route from clearing
           a row by mistake to getting it back — and the note `keep` cannot
           stamp, the one with no frontmatter, is the likeliest mistake. Closed
           by default: it is a correction, not part of the pass. -->
      <details v-if="visibleCleared.length" class="vr-cleared">
        <summary class="vr-cleared-summary">
          Recently cleared ({{ visibleCleared.length }})
        </summary>
        <p class="vr-hint vr-cleared-hint">
          Notes you marked <strong>Still true</strong>. They stay out of the queue until
          the note changes; <strong>Add back</strong> returns one now. A note with no
          frontmatter has nowhere to record the check, so Ciaobot says so when that happens.
        </p>
        <ul class="vr-rows">
          <li
            v-for="note in visibleCleared"
            :key="note.candidate_id"
            class="vr-row"
            :class="{ 'vr-row--busy': store.isBusy(note.candidate_id) }"
          >
            <div class="vr-row-body">
              <h3 class="vr-title">{{ candidateLeaf(note.path) }}</h3>
              <p class="vr-meta">{{ note.path }}<span v-if="clearedDate(note)"> · cleared {{ clearedDate(note) }}</span></p>
            </div>
            <div class="mr-actions vr-actions">
              <button
                type="button"
                class="mr-btn mr-btn--quiet"
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
      <p v-if="showInitialLoading" class="vr-empty" role="status">Loading retired notes…</p>
      <div v-else-if="showInitialError" class="vr-load-state vr-load-state--error" role="alert">
        <span>Could not load retired notes. {{ store.loadError }}</span>
        <button type="button" class="btn-small" @click="refresh">Retry</button>
      </div>
      <p v-else-if="hasCurrentSnapshot && !visibleTrashed.length" class="vr-empty">
        Nothing retired yet. A note you retire from <strong>To revisit</strong>
        waits here until you restore it or delete it for good.
      </p>
      <ul v-else-if="visibleTrashed.length" class="vr-rows">
        <li
          v-for="note in visibleTrashed"
          :key="note.candidate_id"
          class="vr-row"
          :class="{ 'vr-row--busy': store.isBusy(note.candidate_id) }"
        >
          <div class="vr-row-body">
            <h3 class="vr-title">{{ trashedTitle(note) }}</h3>
            <p class="vr-meta">{{ note.original_path }}<span v-if="trashedDate(note)"> · retired {{ trashedDate(note) }}</span></p>
          </div>
          <div class="mr-actions vr-actions">
            <button
              type="button"
              class="mr-btn"
              :disabled="store.isBusy(note.candidate_id)"
              @click="restoreRow(note)"
            >{{ store.isBusy(note.candidate_id) ? 'working…' : 'Restore' }}</button>
            <button
              type="button"
              class="mr-btn mr-btn--quiet vr-danger"
              :disabled="store.isBusy(note.candidate_id)"
              @click="deleteRow(note)"
            >Delete forever</button>
          </div>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped src="./memoryReview.css"></style>

<style scoped>
/* Hairline rows with the actions stacked on the right, the same shape as the
   Suggested list beside it (both take their shared pieces from
   memoryReview.css). */
.vault-review {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  /* Block padding only: the rows start on the page column's edge. */
  padding: var(--space-4) 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.vr-head {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.vr-hint {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.vr-empty {
  margin: 0;
  color: var(--fg2);
  font-size: 0.9rem;
  padding: var(--space-4) 0;
}

/* A correction, not part of the pass: quieter than the queue above it, and
   separated by a rule so the two lists never read as one. */
.vr-cleared {
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}

.vr-cleared-summary {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  cursor: pointer;
  color: var(--fg2);
  font-size: var(--text-sm);
}

.vr-cleared-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

.vr-cleared-hint {
  margin: var(--space-2) 0 var(--space-3);
  max-width: 64ch;
}

.vr-load-state {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.vr-load-state--error {
  border-color: color-mix(in srgb, var(--error) 46%, var(--border));
}

.vr-load-state--stale {
  background: color-mix(in srgb, var(--warning) 8%, var(--bg2));
}

.vr-load-state .btn-small {
  flex: none;
}

.vr-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--border);
}

.vr-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: var(--space-2) var(--space-5, 24px);
  padding: 18px 0;
  border-bottom: 1px solid var(--border);
}

.vr-row--busy {
  opacity: 0.72;
  pointer-events: none;
}

.vr-row-body {
  min-width: 0;
}

.vr-title {
  margin: 0;
  color: var(--fg);
  font-size: calc(15px * var(--font-scale, 1));
  font-weight: 650;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

/* type · reason · backlinks */
.vr-why {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 8px;
  margin: 3px 0 0;
  color: var(--fg3);
  font-size: var(--text-sm);
}

.vr-why-sep { color: var(--fg3); }

/* The reason is a disclosure button. Text stays in the body colour (the
   caution orange does not reach AA as small text in the light theme); the
   dotted caution underline and the chevron carry the signal, and the words
   carry the meaning. */
.vr-flag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  min-height: 24px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
  text-decoration: underline dotted color-mix(in srgb, var(--warning) 80%, transparent);
  text-underline-offset: 3px;
}

.vr-flag:hover { color: var(--fg); text-decoration-style: solid; }

.vr-flag-chev {
  width: 12px;
  height: 12px;
  flex: none;
  fill: none;
  stroke: var(--warning);
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 120ms var(--ease);
}

.vr-flag[aria-expanded='true'] .vr-flag-chev { transform: rotate(90deg); }

@media (pointer: coarse) {
  .vr-flag { min-height: var(--touch); }
}

.vr-evidence { margin-top: var(--space-2); }

.vr-meta {
  margin: 0.25rem 0 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

.vr-badge {
  font-size: var(--text-xs);
  padding: 0.1rem 0.4rem;
  border-radius: var(--radius-xs);
}

.vr-badge.--warn {
  background: color-mix(in srgb, var(--warning) 18%, transparent);
  color: var(--fg);
}

/* The note's own words, visible without a click, clamped to two lines. */
.vr-excerpt-inline {
  margin: var(--space-2) 0 0;
  max-width: 72ch;
  color: var(--fg2);
  font-size: var(--text-base, 14px);
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  overflow-wrap: anywhere;
}

/* The path opens the whole note. */
.vr-path {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  margin-top: 4px;
  padding: 0;
  border: none;
  background: none;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  overflow-wrap: anywhere;
}

.vr-path:hover { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; }
.vr-path:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }

@media (pointer: coarse) {
  .vr-path { min-height: var(--touch); }
}

/* Destructive, but not competing-pink: the quiet button in the error colour. */
.vr-danger {
  color: var(--error);
  border-color: color-mix(in srgb, var(--error) 55%, var(--border));
}

.vr-trash {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

@media (max-width: 640px) {
  .vr-row {
    grid-template-columns: minmax(0, 1fr);
  }

  .vr-load-state {
    align-items: stretch;
    flex-direction: column;
  }
}

@media (prefers-reduced-motion: reduce) {
  .vr-flag-chev { transition: none; }
}
</style>
