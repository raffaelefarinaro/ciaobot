<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useVaultReviewStore } from '../stores/vaultReview'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProposalsStore } from '../stores/proposals'
import { reviewPath } from '../stores/memoryMap'
import type { VaultReviewCandidate, VaultReviewCheck, VaultReviewEntryFinding, VaultReviewEntryProposal, VaultTrashedNote,
  VaultClearedNote } from '../lib/types'
import {
  candidateLeaf, coverageLabel, coverageSummary, entryReasonExplanation, entryReasonLabel, orderedSignals, signalChipLabel, signalLabel, signalReasons, signalRowLabel, verificationLabel, verdictLabel,
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
const proposals = useProposalsStore()
const viewRouter = useRouter()

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
// inline banner would sit above the list with no way to dismiss it. The title
// names the panel, not one of its actions: a failed completion is not a failed
// retirement, and the old wording told the user they had tried to do something
// this row never offered.
//
// The body is the store's plain copy; the engine's own message rides along in
// `errorText`, which is what a fix chat is seeded with. A refusal the store
// translated is therefore never lost, just not read by default.
watch(
  () => store.error,
  (message) => {
    if (!message) return
    projectStore.pushToast({
      chat_id: '',
      title: 'Review action failed',
      body: message,
      variant: 'error',
      errorText: store.errorDetail || message,
    })
    store.error = ''
    store.errorDetail = ''
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

// ── The managed verification ───────────────────────────────────────────────
//
// A note the verification pass has already looked at is not the same kind of
// row as one nobody has touched, and the panel has to be able to say which.
// Three states, all decided on the server from the note's CURRENT revision:
//
// * a proposal is waiting for this exact revision → link to it, and take away
//   this queue's own retirement offer. Two buttons asking the same question in
//   two places, able to disagree, is the thing this replaces.
// * a proposal is pinned to a revision the note has left → it can no longer be
//   applied, so the row's own actions come back and the panel says why the
//   proposal is dead.
// * a verdict was recorded with nothing pending → the check state, in full:
//   when it ran, what it concluded, how much of the note it covered, and when
//   the note may be asked about again.

/** The check state, or null when nobody has checked this revision. */
function checkOf(candidate: VaultReviewCandidate): VaultReviewCheck | null {
  const v = candidate.evidence.verification
  return v && typeof v === 'object' ? v : null
}

/** Whether this row defers to a proposal rather than offering a second answer. */
function hasPendingProposal(candidate: VaultReviewCandidate): boolean {
  return Boolean(candidate.pending_verification?.proposal_id)
}

/** What the entry-level detector found inside this note, or null.
 *
 * The same block the Memory Map node carries and the nightly `stale_entry` pass
 * is built from, so a row here, a node there and tonight's plan cannot disagree
 * about which fact is overdue. `null` for a note with nothing list-shaped in it
 * and for a server older than this client, which are the same thing to render.
 */
function entryCoverageOf(candidate: VaultReviewCandidate) {
  return candidate.evidence.entry_verification ?? null
}

interface EntryGroup { reason: string; entries: VaultReviewEntryFinding[] }

/** This note's overdue facts, grouped by the reason they share.
 *
 * A note's facts were listed one bullet at a time, each row repeating the
 * server's explanation and the note's date in full — so a note whose five facts
 * were all never checked showed the same two paragraphs five times to convey
 * one fact. What a reason means is shared; what each fact *is* is not. Grouping
 * puts the shared half in one head above the facts that share it, and leaves
 * the row carrying only what differs: the excerpt, the section, the line around
 * it, and (for `aged`, the only reason where the number is per fact) the age.
 *
 * First-seen order, and the server's order inside each group, so nothing moves
 * because of this grouping.
 */
function entryGroups(candidate: VaultReviewCandidate): EntryGroup[] {
  const groups: EntryGroup[] = []
  for (const entry of entryCoverageOf(candidate)?.stale_entries ?? []) {
    const group = groups.find(g => g.reason === entry.reason)
    if (group) group.entries.push(entry)
    else groups.push({ reason: entry.reason, entries: [entry] })
  }
  return groups
}

/** The entry this pending proposal is about, so a link can name it.
 *
 * Matched by identity rather than by position: the proposal and the finding are
 * two records of the same fact written by two passes, and the identity is the
 * only thing they agree on.
 */
function entryForProposal(
  candidate: VaultReviewCandidate,
  proposal: VaultReviewEntryProposal,
) {
  return entryCoverageOf(candidate)?.stale_entries.find(e => e.identity === proposal.identity) ?? null
}

/** Pending entry proposals about a fact this note is not currently owing a
 * check on.
 *
 * The complement of :func:`entryForProposal`, and the reason a decision is not
 * only ever shown attached to an overdue finding. A `restamp_entry` filed last
 * week whose entry has since been re-stamped by a whole-note verdict is real and
 * still waiting for an answer — dropping it because the fact is no longer on the
 * list would leave a question in the queue with nothing on this surface saying
 * it is there, which is the one thing the "link the decision, do not duplicate
 * it" rule is for.
 */
function unmatchedEntryProposals(candidate: VaultReviewCandidate): VaultReviewEntryProposal[] {
  return (entryCoverageOf(candidate)?.proposals ?? []).filter(
    link => !entryForProposal(candidate, link),
  )
}

/** A label that opens a sentence, capitalised.
 *
 * `entryReasonLabel` is written in lower case because it is a noun phrase that
 * also appears mid-sentence in the map; here it opens one, and a row that reads
 * "never checked — nobody has recorded…" looks like a rendering bug rather than
 * a claim.
 */
function sentenceCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** What accepting a linked entry proposal would do, in words. *
 * Named by operation because the three do genuinely different things to the
 * file: one rewrites a line, one stamps it, and one removes it while leaving
 * every other fact in the note alone. "Updates the note" would be wrong for all
 * three, and most wrong for the removal.
 */
function entryOperationLabel(operation: string): string {
  if (operation === 'replace_entry') return 'Rewrites this one fact'
  if (operation === 'restamp_entry') return 'Marks this one fact as checked again'
  if (operation === 'retire_entry') return 'Removes this one fact; the note is kept'
  return 'Decides this one fact'
}

/** Open a pending *entry* proposal in Suggested, where its accept lives.
 *
 * The same shape as the note-level link and for the same reason: the decision
 * is already queued, with the entry's exact before/after and the evidence on the
 * card. Offering a second accept on this row would answer one question twice,
 * and the row's own buttons are about the whole file.
 */
async function openEntryProposal(
  candidate: VaultReviewCandidate,
  proposal: VaultReviewEntryProposal,
) {
  await proposals.ensureLoaded()
  proposals.revealRow(proposal.proposal_id)
  await viewRouter.push(reviewPath('suggested'))
}

/**
 * Whether the row still offers Retire / Complete.
 *
 * The server's answer, read off the payload. A client that re-derived it from
 * the signal list could disagree with the engine about what a note's only
 * reason for being here is, and the failure is a row that is either stuck or
 * still asking a question the queue has already handed to somebody.
 */
function offersRetirement(candidate: VaultReviewCandidate): boolean {
  // Absent means an older server, which never suppressed anything. Defaulting to
  // "yes" keeps that server's behaviour exactly as it was.
  return candidate.retirement_offered !== false
}

/** Whether the row offers "Still true" — the pass's own re-stamp.
 *
 * Never while a proposal is pending: it would stamp `updated: today` onto a note
 * somebody was about to rewrite, and the stamp would claim a verification of
 * text the pass had already said was wrong. Still offered on a CONFLICTED row,
 * because there the proposal is dead and this is the only way back.
 */
function offersReverify(candidate: VaultReviewCandidate): boolean {
  return !hasPendingProposal(candidate)
}

/** Open the proposal a row points at, in the queue it lives in.
 *
 * A link rather than an inline duplicate of the card: the proposal carries the
 * before/after, the evidence and the accept button, and a second rendering of
 * it here would be a second copy free to fall out of step with the one that
 * actually applies the change.
 *
 * The queue is loaded *before* the navigation, not alongside it. The reveal
 * waits for the row element to exist, and starting the fetch here would race
 * the route change for no benefit.
 */
async function openPendingProposal(candidate: VaultReviewCandidate) {
  const pending = candidate.pending_verification
  if (!pending?.proposal_id) return
  await proposals.ensureLoaded()
  proposals.revealRow(pending.proposal_id)
  await viewRouter.push(reviewPath('suggested'))
}

// One chat at a time: the button is on every row, and a double-click used to
// be able to open two chats about the same note before the first returned.
const chatBusy = ref(false)

/** What the queue knows about a candidate, as a sentence the agent can read.
 *
 * The row already shows all of it; repeating it in the message means the chat
 * starts from the same evidence the decision is being made on, without the
 * agent having to re-derive why curation flagged the note.
 *
 * The question asked depends on the row's terminal action: a project is not
 * being weighed for retirement, it is being offered a completion, and asking
 * "what would be lost if it went" about a note that is about to move to
 * `projects/completed/` invites the wrong answer.
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
    `I am deciding what to do with \`${candidate.path}\` in the ` +
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
 *
 * Every branch ends by naming the buttons the row actually carries. A
 * completable row has no Retire, and a seed that offers one sends the agent
 * weighing an option the user cannot pick — the same small dishonesty as a
 * title that names a question the draft does not ask.
 */
function discussTask(candidate: VaultReviewCandidate): string {
  if (!candidate.signals.includes('unlinked')) {
    if (isCompletable(candidate)) {
      return (
        'Read the note and tell me whether this project has actually finished, ' +
        'and what would be lost by completing it. Do not edit, move, or delete ' +
        'anything — I will pick Still true or Complete myself.'
      )
    }
    return (
      'Read the note and tell me what would be lost if it went, and whether ' +
      'anything in it belongs somewhere else first. Do not edit, move, or delete ' +
      'anything — I will pick Still true or Retire myself.'
    )
  }
  // The closing half is where the row's terminal action has to be named, and
  // the two differ in the argument as well as the choice: telling the agent
  // that "nothing should link to it" is a reason to RETIRE a project it is
  // about to be offered Complete for would aim the answer at the wrong verdict.
  const verdict = isCompletable(candidate)
    ? 'that is an argument for completing it.'
    : 'that is an argument for retiring it.'
  const closing = isCompletable(candidate)
    ? 'I will pick Still true or Complete myself.'
    : 'I will pick Still true or Retire myself.'
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
    `plainly — ${verdict} Do not edit, move, or delete the note itself; ${closing}`
  )
}

/** The chat's name, following the question the seed actually opens with:
 * "Retire Mo?" over a chat hunting for links to Mo is the same small
 * dishonesty the `Link fixed` button was removed for. The second half of that
 * question is the row's terminal action, so a project row says Complete where
 * every other row says Retire. */
function discussTitle(candidate: VaultReviewCandidate): string {
  const leaf = candidateLeaf(candidate.path)
  if (candidate.signals.includes('unlinked')) {
    return isCompletable(candidate) ? `Link or complete ${leaf}?` : `Link or retire ${leaf}?`
  }
  return isCompletable(candidate) ? `Complete ${leaf}?` : `Retire ${leaf}?`
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
      title: discussTitle(candidate),
      seed: discussPrompt(candidate),
    })
  } finally {
    chatBusy.value = false
  }
}

/**
 * Whether this row offers **Complete** instead of **Retire**.
 *
 * The backend's answer, read straight off the payload: it decides from the
 * same helpers `complete_project_note` gates on, so a button that renders is a
 * click the engine will honour. Re-deriving project-ness here from
 * `evidence.type` would be a second definition free to disagree with the
 * first, and the disagreement shows up as a refusal on a row that still looks
 * actionable.
 */
function isCompletable(candidate: VaultReviewCandidate): boolean {
  return candidate.completable === true
}

async function keepRow(candidate: VaultReviewCandidate) {
  await store.decide(workspace.value, candidate.candidate_id, 'keep')
}

async function trashRow(candidate: VaultReviewCandidate) {
  // No confirm here: trash is reversible and one click restores it. The
  // confirm budget is spent on permanent deletion instead.
  await store.trash(workspace.value, candidate.candidate_id)
}

/**
 * Close a project out, in place of retiring it.
 *
 * Asks first, where `trashRow` does not. The reason trash needs no confirm is
 * that one click restores it, and a completed project has no such route from
 * here: `restore_completed` exists on the engine but nothing in the panel calls
 * it, and a completed note is in neither the candidate list nor the trash to
 * hang a Restore button on. So a misclick rewrites every note that links to the
 * project and leaves no in-app way back — which is exactly what a confirm is
 * for. The wording says so rather than implying the action is free.
 *
 * The confirm also names the unit of the move. A nested candidate is completable
 * in its own right, but completing it closes the PROJECT: the folder holding it
 * travels too, so "moves it" for a note that takes a directory with it reads as
 * a smaller act than it is. The backend answers which case this is
 * (`completion_moves_folder`) rather than the panel re-deriving the layout from
 * the path.
 */
async function completeRow(candidate: VaultReviewCandidate) {
  const title = candidateLeaf(candidate.path)
  const moves = candidate.completion_moves_folder === true
    ? 'The whole project folder moves to projects/completed/'
    : 'It moves to projects/completed/'
  if (!await askConfirm(
    `Complete "${title}"? ${moves} and every note that links to it is rewritten to follow. This cannot be undone from here.`,
    { title: 'Complete project', confirmLabel: 'Complete', destructive: true },
  )) return
  await store.complete(workspace.value, candidate.candidate_id)
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
      <!-- One sentence says what the buttons do; the rows repeat nothing. -->
      <p v-if="props.section !== 'trash'" class="mr-lede vr-lede">
        Saved notes that may have gone out of date. <strong>Still true</strong> marks a note
        checked today. A project offers <strong>Complete</strong> in its place, which moves it
        to <code>projects/completed/</code> and repoints what links to it;
        <strong>Retire</strong> covers every other note, moving it to Retired, where it can be
        restored. Where the nightly pass has already filed a proposal about a note, this list
        links to it instead of asking the same question again.
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
                    <!-- The last recorded check, when there is one. It is NOT the
                         date above: that is the note's own `updated:`, and a
                         check that came back unverified wrote nothing, so the
                         two say different things. Showing only the newer one
                         would let "checked yesterday" and "unchecked for a
                         year" read as a contradiction instead of a verdict
                         nobody could support.

                         Skipped where a pending card below already says all of
                         it: two boxes on one row repeating the same verdict,
                         coverage, citations and reason is a wall, and the one
                         that matters is the card that carries the decision. -->
                    <template v-if="checkOf(candidate) && !hasPendingProposal(candidate)">
                      Ciaobot last checked it on <code>{{ checkOf(candidate)!.checked_at }}</code>:
                      {{ verdictLabel(checkOf(candidate)!.outcome) }}<template
                        v-if="checkOf(candidate)!.coverage"
                      >, covering {{ coverageLabel(checkOf(candidate)!.coverage) }}</template>.<template
                        v-if="checkOf(candidate)!.citations"
                      > Backed by {{ checkOf(candidate)!.citations }} citation{{ checkOf(candidate)!.citations === 1 ? '' : 's' }}.</template>
                      <template v-if="checkOf(candidate)!.reason">{{ checkOf(candidate)!.reason }}</template>
                    </template>
                    <template v-else-if="checkOf(candidate)">
                      Ciaobot has since checked this exact text; the verdict is on
                      the proposal below.
                    </template>
                  </p>

                  <p v-else-if="signal === 'unverified_entries' && entryCoverageOf(candidate)" class="mr-box-body">
                    The note's own date is
                    <template v-if="candidate.evidence.unverified">current, but its facts are not</template>
                    <template v-else>not the whole story</template>.
                    {{ coverageSummary(candidate.evidence) }}.
                  </p>
                  <p v-else-if="signal === 'unverified_entries'" class="mr-box-body">
                    Some facts inside this note have gone unchecked.
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

              <!-- The facts inside the note, one bullet at a time.
                   Always rendered when the detector ran, not only behind the
                   signal's disclosure: an entry that is due and an entry nobody
                   ever checked are different amounts of work, and a card that
                   hides both behind one collapsed chevron makes the row say
                   "3 facts" and leave the reader to guess which is which. The
                   note's own buttons below act on the whole file; everything in
                   here is about one line, and the action for a line is the
                   proposal this block links to. -->
              <div v-if="entryCoverageOf(candidate)" class="vr-entries">
                <p class="vr-entries-head">
                  <strong>Facts inside this note</strong> — {{ coverageSummary(candidate.evidence) }}.
                  <template v-if="entryCoverageOf(candidate)!.uncovered">
                    Some of the note is written as prose or a table, which this check
                    cannot read as facts — so it is never counted as verified.
                  </template>
                </p>
                <!-- One group per reason, each stating that reason once. The two
                     things every fact under a head has in common — why it is
                     here, and (for a fact with no usable stamp of its own) that
                     it is carrying the note's date rather than its own — used to
                     be printed again on every row, so a note whose facts were
                     all never checked said the same thing once per bullet. The
                     per-fact rows below keep only what differs. -->
                <template v-if="entryGroups(candidate).length">
                  <div
                    v-for="group in entryGroups(candidate)"
                    :key="group.reason"
                    class="vr-entry-group"
                  >
                    <p class="vr-entry-reason-head">
                      <strong>{{ sentenceCase(entryReasonLabel(group.reason)) }}</strong><template v-if="entryReasonExplanation(group.reason)"> — {{ entryReasonExplanation(group.reason) }}</template>
                    </p>
                    <ul class="vr-entries-list">
                      <li
                        v-for="entry in group.entries"
                        :key="entry.identity"
                        class="vr-entry"
                      >
                        <!-- The one number that is genuinely per fact, so it
                             cannot live in the group's sentence: a figure there
                             would read as a claim about every fact under it. -->
                        <p v-if="group.reason === 'aged' && entry.age_days !== null" class="vr-entry-age">
                          unverified for {{ entry.age_days }}d
                        </p>
                        <blockquote v-if="entry.section" class="vr-entry-section">in “{{ entry.section }}”</blockquote>
                        <pre class="vr-entry-text"><code>{{ entry.excerpt }}</code></pre>
                        <p v-if="entry.context.length" class="vr-entry-context">
                          <span v-for="(line, li) in entry.context" :key="li" class="vr-entry-context-line">{{ line }}</span>
                        </p>
                        <p
                          v-for="link in entryCoverageOf(candidate)!.proposals.filter(p => p.identity === entry.identity)"
                          :key="link.proposal_id"
                          class="vr-entry-decision"
                        >
                          <template v-if="link.conflicted">
                            <span class="vr-entry-conflict">
                              A decision for this fact was filed against text the note no longer
                              holds, so it cannot be applied — Ciaobot will file a new one.
                            </span>
                          </template>
                          <template v-else>
                            Waiting in Suggested: {{ entryOperationLabel(link.operation) }} —
                            checked <code>{{ link.checked_at }}</code><template v-if="link.citations">
                              , on {{ link.citations }} citation{{ link.citations === 1 ? '' : 's' }}</template>.
                          </template>
                          <button
                            type="button"
                            class="mr-link vr-pending-link"
                            title="Open the proposal in Suggested, where the change and its evidence are"
                            @click="openEntryProposal(candidate, link)"
                          >Open the proposal</button>
                        </p>
                      </li>
                    </ul>
                  </div>
                </template>
                <p v-else-if="entryCoverageOf(candidate)!.checked" class="vr-hint">
                  Every fact written as a list item in this note is current.
                </p>
                <p v-if="entryCoverageOf(candidate)!.more_stale_entries" class="vr-hint">
                  And {{ entryCoverageOf(candidate)!.more_stale_entries }} more.
                </p>

                <!-- A decision waiting on somebody, about an entry this note
                     does not currently owe a check on. A `restamp_entry` filed
                     yesterday whose entry has since been re-stamped by a
                     whole-note verdict is exactly this, and dropping it would
                     leave a question in the queue with nothing on the surface
                     that says it is there. Named by its own identity rather than
                     shown against a finding, because the honest statement is
                     "about a fact that is not on the overdue list" — not a
                     fabricated row for a fact nobody has flagged. -->
                <p
                  v-if="unmatchedEntryProposals(candidate).length"
                  class="vr-entries-head vr-entries-head--waiting"
                >
                  <strong>Decisions waiting on you</strong> — about
                  {{ unmatchedEntryProposals(candidate).length }}
                  {{ unmatchedEntryProposals(candidate).length === 1 ? 'fact' : 'facts' }}
                  in this note that {{ unmatchedEntryProposals(candidate).length === 1 ? 'is' : 'are' }}
                  not on the overdue list.
                </p>
                <p
                  v-for="link in unmatchedEntryProposals(candidate)"
                  :key="link.proposal_id"
                  class="vr-entry-decision"
                >
                  <template v-if="link.conflicted">
                    <span class="vr-entry-conflict">
                      A decision for this fact was filed against text the note no longer
                      holds, so it cannot be applied — Ciaobot will file a new one.
                    </span>
                  </template>
                  <template v-else>
                    Waiting in Suggested: {{ entryOperationLabel(link.operation) }} —
                    checked <code>{{ link.checked_at }}</code><template v-if="link.citations">
                      , on {{ link.citations }} citation{{ link.citations === 1 ? '' : 's' }}</template>.
                  </template>
                  <button
                    type="button"
                    class="mr-link vr-pending-link"
                    title="Open the proposal in Suggested, where the change and its evidence are"
                    @click="openEntryProposal(candidate, link)"
                  >Open the proposal</button>
                </p>

                <p v-if="entryCoverageOf(candidate)!.more_proposals" class="vr-hint">
                  {{ entryCoverageOf(candidate)!.more_proposals }} more decision(s) are queued in Suggested.
                </p>
              </div>

              <!-- A pending proposal, in place of this queue's own decision.
                   The link is the whole point: the question is already in the
                   Suggested queue, with the before/after and the evidence on
                   the card, and answering it here as well would be two answers
                   to one revision. -->
              <div v-if="hasPendingProposal(candidate)" class="vr-pending">
                <p class="vr-pending-head">
                  <strong>Verification proposal pending</strong> — Ciaobot checked this
                  revision on <code>{{ candidate.pending_verification!.checked_at }}</code> and
                  concluded {{ verdictLabel(candidate.pending_verification!.outcome) }}<template
                    v-if="candidate.pending_verification!.coverage"
                  >, covering {{ coverageLabel(candidate.pending_verification!.coverage) }}</template>.<template
                    v-if="candidate.pending_verification!.citations"
                  > Backed by {{ candidate.pending_verification!.citations }} citation{{ candidate.pending_verification!.citations === 1 ? '' : 's' }}.</template>
                </p>
                <p v-if="candidate.pending_verification!.reason" class="vr-pending-reason">
                  {{ candidate.pending_verification!.reason }}
                </p>
                <button
                  type="button"
                  class="mr-link vr-pending-link"
                  title="Open the proposal in Suggested, where the change and its evidence are"
                  @click="openPendingProposal(candidate)"
                >Open the proposal</button>
                <p class="vr-hint vr-pending-hint">
                  The decision is on the proposal, not here — accepting or dismissing it
                  applies or declines the change. This note stays in the list until that
                  happens.
                </p>
              </div>

              <!-- A proposal pinned to text this note no longer holds. The
                   proposal cannot be applied (its accept refuses as a conflict),
                   so this row's own actions are the only route left, and the
                   panel says which revision they are looking at. -->
              <p v-else-if="checkOf(candidate)?.conflicted" class="vr-conflict">
                A verification proposal was filed for an earlier version of this note, so it
                can no longer be applied — Ciaobot will file a new one. Until then the note
                is yours to decide here.
              </p>

              <p v-if="inlineExcerpt(candidate)" class="vr-excerpt-inline">{{ inlineExcerpt(candidate) }}</p>
              <button
                type="button"
                class="vr-path"
                :title="`Open ${candidate.path}`"
                @click="openNote(candidate.path)"
              >{{ candidate.path }}</button>
            </div>

            <div class="mr-actions vr-actions">
              <!-- Two offers that vanish together on a pending row, and only on
                   one. `Still true` would stamp the note as verified today,
                   which is a claim about text the pass has already said is
                   wrong; `Retire` would answer, in a second place, the question
                   the proposal is holding. -->
              <button
                v-if="offersReverify(candidate)"
                type="button"
                class="mr-btn"
                :disabled="store.isBusy(candidate.candidate_id)"
                title="Clear the row, and stamp the note's updated date as today when it has frontmatter to stamp"
                @click="keepRow(candidate)"
              >{{ store.isBusy(candidate.candidate_id) ? 'working…' : 'Still true' }}</button>
              <!-- Complete and Retire are the same slot, never both on one row.
                   Retire is deliberately unreachable on a project row: it replaces
                   the trash, and a project is closed out rather than hidden, so
                   the confirm is where the weight sits instead. -->
              <template v-if="offersRetirement(candidate)">
                <button
                  v-if="isCompletable(candidate)"
                  type="button"
                  class="mr-btn"
                  :disabled="store.isBusy(candidate.candidate_id)"
                  title="Close this project out: move it to projects/completed/ and repoint every note that links to it"
                  @click="completeRow(candidate)"
                >{{ store.isBusy(candidate.candidate_id) ? 'working…' : 'Complete' }}</button>
                <button
                  v-else
                  type="button"
                  class="mr-btn mr-btn--quiet"
                  :disabled="store.isBusy(candidate.candidate_id)"
                  @click="trashRow(candidate)"
                >Retire</button>
              </template>
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

/* A note the pass is holding for a person. The box is the row's reason for
   being here now, so it takes the same quiet treatment as the evidence boxes
   above — a rule and a tinted ground, not a second card. */
.vr-pending {
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg2);
}

.vr-pending-head {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.vr-pending-head strong { color: var(--fg); }

.vr-pending-reason {
  margin: 0.35rem 0 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

/* The whole point of the box: a link, not a button styled like one. */
.vr-pending-link {
  margin-top: var(--space-2);
  min-height: 24px;
}

.vr-pending-hint {
  margin-top: 4px;
  max-width: 60ch;
}

@media (pointer: coarse) {
  .vr-pending-link { min-height: var(--touch); }
}

/* The facts inside a note. Its own border colour from the pending box, because
   it is the same kind of thing: something decided elsewhere, shown here so the
   row is not a bare verdict. Everything in it is one line of one file, so the
   type is deliberately smaller and quieter than the row's own heading — a
   fifty-bullet note must not read as fifty paragraphs of chrome. */
.vr-entries {
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-left: 3px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg2);
}

.vr-entries-head {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.vr-entries-head strong { color: var(--fg); }

/* The waiting sub-heading. Accent-edged rather than a second box, because it is
   a continuation of the same list rather than a new fact about the note. */
.vr-entries-head--waiting {
  margin-top: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
}

/* One reason, then the facts that share it. The head is the only place a
   reason is explained, so a second group needs air above it rather than a
   border: the reader is being told something new, not shown a continuation. */
.vr-entry-group + .vr-entry-group { margin-top: var(--space-3); }

.vr-entries-list {
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.vr-entry {
  padding-left: var(--space-2);
  border-left: 2px solid var(--border);
}

.vr-entry-reason-head,
.vr-entry-age,
.vr-entry-section,
.vr-entry-context,
.vr-entry-decision {
  margin: 0;
  font-size: var(--text-sm);
  line-height: 1.5;
}

.vr-entry-reason-head { color: var(--fg2); }
.vr-entry-reason-head strong { color: var(--fg); }
/* The per-fact age is the only number left on a row, so it reads as metadata
   beside the excerpt rather than as a second claim about the fact. */
.vr-entry-age,
.vr-entry-section { color: var(--fg3); }
.vr-entry-section { font-style: italic; }

.vr-entry-text {
  margin: 4px 0 0;
  padding: var(--space-1) var(--space-2);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  font-size: var(--text-sm);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  /* The excerpt is capped server-side; this is belt and braces against a note
     whose single bullet is a paragraph, which would otherwise be a wall. */
  max-height: 9rem;
  overflow-y: auto;
}

.vr-entry-context {
  margin-top: 4px;
  color: var(--fg3);
  display: flex;
  flex-direction: column;
}

.vr-entry-context-line {
  font-size: var(--text-xs, 0.75rem);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.vr-entry-decision {
  margin-top: 4px;
  color: var(--fg2);
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1);
}

.vr-entry-conflict { color: var(--fg2); font-style: italic; }

.vr-entry-decision .vr-pending-link { margin-top: 0; }

@media (pointer: coarse) {
  .vr-entry-decision .vr-pending-link { min-height: var(--touch); }
}

/* A proposal whose revision the note has left. Warning-coloured, because it
   explains why the pipeline stopped and the row is actionable again — which is
   the opposite of a row that is merely busy. */
.vr-conflict {
  margin: var(--space-2) 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
  padding-left: var(--space-2);
  border-left: 3px solid color-mix(in srgb, var(--warning) 70%, transparent);
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
