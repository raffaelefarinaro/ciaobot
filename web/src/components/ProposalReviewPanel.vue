<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { DropdownMenuContent, DropdownMenuItem, DropdownMenuRoot, DropdownMenuTrigger } from 'reka-ui'
import { askPrompt } from '../lib/prompt'
import { useProposalsStore } from '../stores/proposals'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import type { ProposalAcceptRefusal, ProposalPreview, ProposalRow } from '../lib/types'
import { lineChanges, type LineChange } from '../lib/textDiff'
import { canReconcile, descriptorFor, kindLabel, rehomeMode } from '../lib/proposalKinds'
import type { ProposalMergeFallback } from '../lib/proposalKinds'
import ProposalHistoryList from './ProposalHistoryList.vue'

const store = useProposalsStore()
const projectStore = useProjectStore()
const fileViewer = useFileViewerStore()

// Failures go to the app's error toast, not to a red line above the list. The
// inline element sat between the hint and the rows, pushed everything down, and
// stayed there with no way to dismiss it — so a stale message about one row read
// as a problem with the whole queue.
watch(
  () => store.error,
  (message) => {
    if (!message) return
    projectStore.pushErrorToast('Proposal action failed', message)
    store.error = ''
  },
)
const chatBusy = ref(false)

type ProposalHelper = NonNullable<import('../lib/types').ChatInfo['helper']>

function resolutionHelper(...proposalIds: string[]): ProposalHelper {
  return {
    kind: 'proposal',
    intent: 'resolve',
    proposal_ids: proposalIds,
    archive_policy: 'when_resolved',
  }
}

function reviewHelper(...proposalIds: string[]): ProposalHelper {
  return {
    kind: 'proposal',
    intent: 'review',
    proposal_ids: proposalIds,
    archive_policy: 'manual',
  }
}

const olderThanDays = ref(30)

/**
 * Rows whose last accept was deferred: the fact may supersede something the
 * region already holds and the reconcile could not say what, so nothing was
 * written and the row is still queued.
 *
 * Held per row rather than in `store.error` because it is not an error to read
 * and dismiss — it is a state the row is in, with its own next step. A toast
 * would take the reason and the competing entries away the moment they became
 * relevant, and the retry that resolves it belongs next to them.
 */
const deferredById = ref<Record<string, { reason: string; competing: string[]; text?: string; expectedRevision?: string }>>({})

function deferredFor(row: ProposalRow) {
  return deferredById.value[row.id]
}

function clearDeferred(id: string) {
  if (!(id in deferredById.value)) return
  const next = { ...deferredById.value }
  delete next[id]
  deferredById.value = next
}

// -- Decision card ----------------------------------------------------------
//
// Accept used to write straight from the row, which meant the only description
// of the change was the bullet's own text — and that is not the change: the
// promotion reconciles against whatever the destination holds now, stamps a
// learned-at date, recognises a duplicate and writes nothing, or bumps a
// learning's recurrence count instead of appending a second copy.
//
// So the primary action opens a card instead: destination, source, the Add /
// Update operation, and the exact replacement the SERVER computed. One primary
// action confirms it; edit, discuss and dismiss are the secondaries. The card
// replaces the row's actions in place, the same shape the leak confirmation
// already used (which now folds into the card rather than stacking a second
// confirmation on top of it).
const previewId = ref('')
const editingPreview = ref(false)
const editBuffer = ref('')

const openPreview = computed(() => (previewId.value ? store.previews[previewId.value] : undefined))

/** Which row, if any, is showing its decision card. */
function isPreviewOpen(row: ProposalRow): boolean {
  return previewId.value === row.id
}

async function reviewAccept(row: ProposalRow) {
  previewId.value = row.id
  editingPreview.value = false
  editBuffer.value = row.text
  await store.loadPreview(row.id)
}

function closePreview() {
  const id = previewId.value
  previewId.value = ''
  editingPreview.value = false
  editBuffer.value = ''
  // The revision is only a promise about a card the operator is looking at.
  // Leaving it behind would hand a stale one to the next batch accept.
  if (id) store.dropPreview(id)
}

function startEditingPreview() {
  editingPreview.value = true
  editBuffer.value = openPreview.value?.text || editBuffer.value
}

/** Re-preview the edited wording against the same current destination, so the
 * replacement on screen is always the one the accept would make. */
async function applyPreviewEdit() {
  const id = previewId.value
  if (!id) return
  const text = editBuffer.value.trim()
  if (!text) return
  await store.loadPreview(id, text)
  editingPreview.value = false
}

function cancelPreviewEdit() {
  editingPreview.value = false
  editBuffer.value = openPreview.value?.text || ''
}

/** The words on the one primary action. Naming the destination is the point:
 * "accept" does not say that a fact is about to enter always-loaded memory. */
function previewPrimaryLabel(row: ProposalRow): string {
  const preview = store.previews[row.id]
  if (!preview) return 'Save'
  if (preview.operation === 'none') return 'Clear this row'
  if (preview.operation === 'move') return `Move to ${preview.destination || 'destination'}`
  return `Save to ${preview.destination || 'memory'}`
}

const OPERATION_LABELS: Record<string, string> = {
  add: 'Add',
  update: 'Update',
  move: 'Move',
  none: 'No change',
}

function operationLabel(operation: string): string {
  return OPERATION_LABELS[operation] ?? 'Change'
}

/** The exact lines the accept would add or remove, from the server's bodies. */
function previewChanges(preview: ProposalPreview): LineChange[] {
  return lineChanges(preview.before, preview.after, preview.separator || '\n')
}

/** Confirm the change the card is showing, pinned to the revision it was
 * computed against. A destination that moved since then comes back as a
 * conflict with a refreshed preview, and the card stays open on it. */
async function confirmPreview(row: ProposalRow, workspace = '', reconcile = false) {
  const preview = store.previews[row.id]
  if (!preview) return
  const edited = preview.text !== row.text ? preview.text : ''
  const result = await store.act(row.id, 'accept', workspace, {
    expectedRevision: preview.revision,
    text: edited,
    reconcile,
  })
  if (result.conflict) return
  if (result.ok) {
    previewId.value = ''
    editingPreview.value = false
    store.dropPreview(row.id)
    clearDeferred(row.id)
    return
  }
  // A refusal that is not a conflict (an over-cap guard, a people note that
  // needs a manual merge) still has the kind's merge-chat fallback behind it.
  // The pending edit rides along into the deferral: without it the retry
  // below would resend the original bullet, silently replacing the wording
  // the operator just approved.
  closePreview()
  await handleAcceptRefusal(row, result.error || '', result.payload, {
    text: edited || undefined,
    expectedRevision: preview.revision,
  })
}

// Proposal → chat link: when an accept fallback or skill implement spawns a
// chat, the row stays queued while the agent works. Remembering that chat
// lets the row show "open chat" instead of the same accept/dismiss buttons
// until the chat is archived/deleted or the proposal disappears, at which
// point we revert to the normal actions.
const PROPOSAL_CHAT_KEY = 'ciao:proposal-chat-links'
// "Talk about it" chats are linked separately: the row still needs a decision
// from the operator, so a discussion keeps the accept/dismiss buttons and only
// swaps "talk about it" for a way back into the chat it already opened.
const PROPOSAL_DISCUSS_KEY = 'ciao:proposal-discuss-links'

function loadProposalChatLinks(key = PROPOSAL_CHAT_KEY): Record<string, string> {
  try {
    const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === 'string' && v) out[k] = v
    }
    return out
  } catch { return {} }
}

const proposalChatLinks = ref<Record<string, string>>(loadProposalChatLinks())

watch(proposalChatLinks, (value) => {
  try { localStorage.setItem(PROPOSAL_CHAT_KEY, JSON.stringify(value)) } catch { /* ignore */ }
}, { deep: true })

const proposalDiscussLinks = ref<Record<string, string>>(loadProposalChatLinks(PROPOSAL_DISCUSS_KEY))

watch(proposalDiscussLinks, (value) => {
  try { localStorage.setItem(PROPOSAL_DISCUSS_KEY, JSON.stringify(value)) } catch { /* ignore */ }
}, { deep: true })

function liveChat(chatId: string | undefined) {
  if (!chatId) return undefined
  const chat = projectStore.chats.find(c => c.chat_id === chatId)
  return chat && !chat.archived ? chat : undefined
}

/** The still-open "talk about it" chat for this row, if there is one. */
function discussionChat(row: ProposalRow) {
  return liveChat(proposalDiscussLinks.value[row.id])
}

function linkDiscussion(rowIds: string[], chatId: string) {
  if (!chatId) return
  const next = { ...proposalDiscussLinks.value }
  for (const id of rowIds) next[id] = chatId
  proposalDiscussLinks.value = next
}

async function openChat(chatId: string) {
  // Mirror InAppToast's workspace hop: the chat lives in its own workspace,
  // which may not be the one the review list is currently scoped to.
  const project = projectStore.projectFor(chatId)
  if (project && project.workspace !== projectStore.activeWorkspace) {
    await projectStore.switchWorkspace(project.workspace)
  }
  await projectStore.switchChat(chatId)
}

async function openDiscussion(row: ProposalRow) {
  const chat = discussionChat(row)
  if (chat) await openChat(chat.chat_id)
}

function linkedChatId(rowId: string): string | undefined {
  return proposalChatLinks.value[rowId]
}

function linkedChat(rowId: string) {
  const id = linkedChatId(rowId)
  if (!id) return undefined
  return projectStore.chats.find(c => c.chat_id === id)
}

function hasActiveLink(row: ProposalRow): boolean {
  const chat = linkedChat(row.id)
  return !!chat && !chat.archived
}

function linkedChatTitle(row: ProposalRow): string {
  const chat = linkedChat(row.id)
  return chat?.title || 'chat'
}

function linkProposal(rowId: string, chatId: string) {
  if (!chatId) return
  proposalChatLinks.value = { ...proposalChatLinks.value, [rowId]: chatId }
}

function clearLink(rowId: string) {
  if (!(rowId in proposalChatLinks.value)) return
  const next = { ...proposalChatLinks.value }
  delete next[rowId]
  proposalChatLinks.value = next
}

async function openLinkedChat(row: ProposalRow) {
  const chatId = linkedChatId(row.id)
  if (!chatId) return
  const chat = projectStore.chats.find(c => c.chat_id === chatId)
  if (!chat || chat.archived) {
    clearLink(row.id)
    return
  }
  await openChat(chatId)
}

function prunedLinks(links: Record<string, string>): Record<string, string> | null {
  // Before the queue and the chat list have loaded, every link looks dead: on a
  // reload the mount-time prune used to drop them all before either arrived.
  // The render already hides a link whose chat is missing, so waiting is safe.
  const rowsKnown = store.loaded
  const chatsKnown = projectStore.chats.length > 0
  if (!rowsKnown && !chatsKnown) return null
  const liveIds = new Set(store.rows.map(r => r.id))
  const next = { ...links }
  let changed = false
  for (const pid of Object.keys(next)) {
    if ((rowsKnown && !liveIds.has(pid)) || (chatsKnown && !liveChat(next[pid]))) {
      delete next[pid]
      changed = true
    }
  }
  return changed ? next : null
}

function pruneProposalChatLinks() {
  const chats = prunedLinks(proposalChatLinks.value)
  if (chats) proposalChatLinks.value = chats
  const discussions = prunedLinks(proposalDiscussLinks.value)
  if (discussions) proposalDiscussLinks.value = discussions
}

watch(() => store.rows.map(r => r.id).join(','), pruneProposalChatLinks)
// A deferral describes a row; a row that left the queue (resolved elsewhere, or
// dismissed here) has no state left to describe, and leaving the notice behind
// would attach it to whatever row the id is next reused for.
watch(() => store.rows.map(r => r.id).join(','), () => {
  const live = new Set(store.rows.map(r => r.id))
  for (const id of Object.keys(deferredById.value)) {
    if (!live.has(id)) clearDeferred(id)
  }
})
watch(() => projectStore.chats.map(c => `${c.chat_id}:${c.archived}`).join(','), pruneProposalChatLinks)

// Filter and selection live in the store: the sidebar renders the controls, the
// way it does for the memory map's categories, and this panel renders the list
// they act on. See `stores/proposals.ts`.
const selected = computed({
  get: () => store.selected,
  set: (value: Set<string>) => { store.selected = value },
})

/** Rows for the workspace the sidebar has selected, then its kind and search
 * filters.
 *
 * Scoped rather than grouped: the workspace switcher on the left is where every
 * other page keeps this choice, and grouping in the list meant the workspace —
 * which decides where an accept writes — lived in a heading you had to scroll
 * back to. The scope rule itself is in the store, so the sidebar's chip counts
 * and this list cannot disagree about what is in scope.
 */
const filtered = computed(() => store.visibleRows(projectStore.activeWorkspace))

// -- Queue load states ------------------------------------------------------
//
// The queue used to render one empty state — "Nothing queued here." — whenever
// `filtered` was empty, so the first (slow) GET, a failed first GET, and a
// filter that matched nothing all looked like a successfully reviewed queue.
// These four are distinct and must stay distinct: a load that has not answered
// yet, a load that failed before any snapshot, a filter hiding a non-empty
// scope, and a queue that really is empty.

/** No snapshot has loaded and none has failed yet: the first fetch is about to
 * start or is in flight. Keyed off `loaded`/`loadError` rather than `loading`,
 * so the render between mount and `onMounted` cannot flash a zero-state. */
const queueLoading = computed(() => !store.loaded && !store.loadError)

/** The list could not be read and there is no snapshot to fall back on, so no
 * empty-queue claim may be made. */
const queueFailed = computed(() => Boolean(store.loadError) && !store.loaded)

/** The first load is over, one way or the other, so rows may be rendered.
 * The rows block and the empty-state block share this and differ only by the
 * stale-refresh clause — spelling the shared half out twice let a later edit
 * invert one branch and not the other. */
const queueSettled = computed(() => !queueLoading.value && !queueFailed.value)

/** Rows in the current workspace scope before the kind/search filters. Lets
 * "nothing matches the filter" be told apart from "nothing queued", the way
 * the History list already does. */
const scopedCount = computed(() => store.scopedRows(projectStore.activeWorkspace).length)

/** A kind/search filter is hiding a non-empty scope. When `filtered` is empty
 * but `scopedCount` is not, a filter must be active: both are computed from
 * the same rows, and with no filter the two are equal. */
const filtersHideEverything = computed(
  () => !filtered.value.length && scopedCount.value > 0,
)

/** The last successful snapshot held nothing in scope, with no failed refresh
 * on top. "All reviewed." may only describe a load that actually succeeded;
 * while a refresh is failing the stale banner speaks instead. */
const queueEmpty = computed(() => store.loaded && !store.loadError && scopedCount.value === 0)

function retryQueue() {
  void store.fetch({ force: true })
}

// -- Queue / History sections -----------------------------------------------
//
// The two sub-views share this panel (and its workspace/kind/search filter
// state in the store) rather than living on separate routes: switching is a
// glance, not a navigation, and the sidebar's scope picker must not reset.
//
// The tab bar that used to pick between them is gone from here. Review had
// three stacked tab rows — Proposals/Retirements, then this one, then To
// review/Trash — so the parent now renders one bar for all four sections and
// drives this panel through `section`. The store still holds the choice, so
// anything that sets `store.view` directly keeps working.
const props = withDefaults(defineProps<{ section?: 'queue' | 'history' }>(), {
  section: 'queue',
})

watch(
  () => props.section,
  (section) => {
    store.view = section
    if (section === 'history') void store.ensureHistoryLoaded(projectStore.activeWorkspace)
  },
  { immediate: true },
)

/** Which section to render. Reads the store, which the watcher above keeps in
 * step with the prop, so a standalone mount (and anything that still flips
 * `store.view` directly) behaves.
 *
 * Deliberately NOT named `section`: a prop and a computed of the same name
 * both land on the instance, so `section` in the template resolved to
 * whichever won rather than to the one meant here. `vue/no-dupe-keys` is an
 * error for exactly that reason. */
const activeSection = computed(() => store.view)

/** A skill proposal's name without its legacy date prefix. New Skill reflection
 * runs upsert one canonical file; grouping keeps older queues understandable
 * until each skill is reflected again.
 */
function skillBase(row: ProposalRow): string {
  return row.text.replace(/^\d{4}-\d{2}-\d{2}-/, '') || row.text
}

/** The date a skill proposal was made, from its filename prefix. */
function skillDate(row: ProposalRow): string {
  return /^(\d{4}-\d{2}-\d{2})-/.exec(row.text)?.[1] ?? ''
}

/** Rows in display order, grouped when grouping means something.
 *
 * Only skill rows group: they repeat by design. A memory bullet or a re-home row
 * is one fact about one note, so grouping those would invent a relationship.
 */
const groups = computed(() => {
  const skills = filtered.value.filter(isSkill)
  const rest = filtered.value.filter(r => !isSkill(r))
  const byName = new Map<string, ProposalRow[]>()
  for (const row of skills) {
    const key = skillBase(row)
    const bucket = byName.get(key)
    if (bucket) bucket.push(row)
    else byName.set(key, [row])
  }
  const skillGroups = [...byName.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([label, rows]) => ({
      key: `skill:${label}`,
      label,
      rows: [...rows].sort((a, b) => skillDate(b).localeCompare(skillDate(a))),
    }))
  const others = rest.length ? [{ key: 'other', label: '', rows: rest }] : []
  return [...others, ...skillGroups]
})


/** The one line that says what this row is about.
 *
 * A re-home bullet is a paragraph of prose that reprints both paths and a CLI
 * incantation; showing it as the title made four rows fill the screen and buried
 * the only thing that differs between them, which is the person's name.
 */
function rowTitle(row: ProposalRow): string {
  if (isRehome(row)) {
    const note = row.rehome?.note ?? ''
    const leaf = note.split('/').pop() ?? ''
    return leaf.replace(/\.md$/, '') || 'a person note'
  }
  return row.text
}

/** Split text on `backtick` spans so they can render as inline code. */
function inlineCodeParts(text: string): Array<{ text: string; code: boolean }> {
  const parts: Array<{ text: string; code: boolean }> = []
  const re = /`([^`]+)`/g
  let last = 0
  let match: RegExpExecArray | null
  while ((match = re.exec(text))) {
    if (match.index > last) parts.push({ text: text.slice(last, match.index), code: false })
    parts.push({ text: match[1], code: true })
    last = match.index + match[0].length
  }
  if (last < text.length) parts.push({ text: text.slice(last), code: false })
  return parts
}

/** Where an accept would write, as a path. Now the tooltip and the details
 * line rather than the row's own subtitle.
 *
 * Every kind's answer lives in its descriptor, including the re-home row's
 * `from → to · why` form and the skill row's path.
 */
function rowSubtitle(row: ProposalRow): string {
  return descriptorFor(row).destination(row)
}

/** The line under the title: what keeping this row would do, named without a
 * path. Same registry, so a new kind still answers both in one place. */
function rowConsequence(row: ProposalRow): string {
  return descriptorFor(row).consequence(row)
}

/** The verbose original, kept behind a disclosure rather than on the surface. */
function rowDetail(row: ProposalRow): string {
  if (isRehome(row)) return row.text
  return row.source ? `from ${row.source}` : ''
}

/** Whether an accept can do what it says. Each kind's descriptor decides. */
function canAccept(row: ProposalRow): boolean {
  return descriptorFor(row).canAccept(row)
}

const allSelected = computed(() =>
  filtered.value.length > 0 && filtered.value.every(r => selected.value.has(r.id)),
)

function toggleAll() {
  const next = new Set(selected.value)
  if (allSelected.value) {
    filtered.value.forEach(r => next.delete(r.id))
  } else {
    filtered.value.forEach(r => next.add(r.id))
  }
  selected.value = next
}

function toggleRow(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

/** Selected rows that are actually on screen.
 *
 * Every batch action is scoped through this, never through the raw `selected`
 * set: the set lives in the store and survives a workspace or kind change,
 * while `visibleRows` filters client-side and `pruneSelected` only drops ids the
 * server stopped returning. So "select all" in `work`, switch to `personal`,
 * press dismiss, and the batch discarded work rows the user could no longer
 * see. A batch may only touch what the list is showing.
 */
const selectedVisible = computed(() => filtered.value.filter(r => selected.value.has(r.id)))

/** Selected rows an accept can actually be performed on.
 *
 * The same predicate as a row's own accept button, and it has to be: the batch
 * bar offered "accept 1" for a re-home row whose own actions correctly showed no
 * accept at all, and accepting one drops the bullet while moving nothing — so a
 * batch could silently discard proposals the UI had just said it could not act
 * on. A skill row is excluded for the older reason: it is a file, not a bullet,
 * and `accept_for('skill')` raises on the server.
 */
const selectedAcceptable = computed(
  () => selectedVisible.value.filter(canAccept).map(r => r.id),
)

function isRehome(row: ProposalRow): boolean {
  return row.kind === 'rehome'
}

function isSkill(row: ProposalRow): boolean {
  return row.kind === 'skill'
}

async function doAccept(row: ProposalRow, workspace = '') {
  await acceptWithFallback(row, workspace)
}

/** Confirm the card's change, but reconcile it against the destination first.

 * Same write as the primary action, with one check in front of it: a fact that
 * supersedes an entry already in the region is merged over it instead of added
 * beside it. Opt-in per click because the server spends a model call on it, and
 * because the plain save is the routine decision.
 *
 * The leak warning needs no separate confirmation: it rides on the card, so the
 * click that releases this write is already the consent for it.
 */
async function reconcileFirst(row: ProposalRow) {
  await confirmPreview(row, '', true)
}

/** Retry a deferred accept, reconciling against the region as it stands now.
 *
 * No further confirmation: this is only reachable from a row that already
 * refused an accept the operator confirmed on the card, so the consent it would
 * ask for has been given for this exact write. The stashed edit and revision
 * go back with it, so the retry cannot land the original bullet over the
 * approved wording, nor land on a destination nobody looked at.
 */
async function retryReconcile(row: ProposalRow) {
  const pending = deferredFor(row)
  const result = await store.act(row.id, 'accept', '', {
    reconcile: true,
    expectedRevision: pending?.expectedRevision,
    text: pending?.text,
  })
  if (result.ok) {
    clearDeferred(row.id)
    return
  }
  if (result.conflict) {
    // The store already adopted the refreshed preview; show it. The deferred
    // card stays behind it would offer a second "try again" against a
    // revision the server just refused.
    clearDeferred(row.id)
    previewId.value = row.id
    return
  }
  await handleAcceptRefusal(row, result.error || '', result.payload)
}

/** The deferral behind a 409, or null when the refusal was some other kind.
 *
 * Read off the error's payload rather than matched against its message: the
 * message is prose meant for a person, and a UI that switches behaviour on it
 * changes meaning the next time the sentence is reworded.
 */
function deferralFrom(payload: unknown): { reason: string; competing: string[] } | null {
  const refusal = payload as ProposalAcceptRefusal | null | undefined
  if (!refusal || typeof refusal !== 'object' || !refusal.deferred) return null
  return {
    reason: refusal.reason || 'the reconcile could not decide',
    competing: Array.isArray(refusal.competing) ? refusal.competing.map(String) : [],
  }
}

async function acceptWithFallback(row: ProposalRow, workspace = '') {
  // Direct accept is best-effort by design – create-only for people
  // (`ciao/memory_proposals.py:348` + `ciao/web/routes_api.py:7559`),
  // fold-guard for projects (`ciao/web/routes_api.py:7602`), cap/region
  // for memory (`ciao/web/routes_api.py:7518`). When it refuses, falling
  // back to a chat that merges keeps the prompt self-contained and nothing
  // breaks – the row stays queued until the merge lands. Which refusals are
  // expected, and what the merge chat is told, is the kind's descriptor.
  const { api } = await import('../lib/api')
  store.setBusy(row.id, true)
  try {
    const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''
    await api.post(`/api/proposals/${row.id}/accept${query}`)
    clearDeferred(row.id)
    await store.fetch({ force: true })
    // `store.fetch` deliberately leaves history alone, so this direct post -
    // the only mutation that does not go through `store.act` - has to say so
    // itself. Without it the accepted decision and the tab badge stayed stale
    // until the next mutation or workspace switch.
    store.invalidateHistory()
    return
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    await handleAcceptRefusal(row, msg, (e as { payload?: ProposalAcceptRefusal })?.payload)
    return
  } finally {
    store.setBusy(row.id, false)
  }
}

/** What to do with an accept the server refused.
 *
 * Shared with the decision card's confirm, so a refusal reached through the
 * preview gets the same merge-chat fallback a direct accept always got —
 * otherwise adding the card would have quietly removed the recovery path for
 * a people note that needs a manual merge or a fold the guard rejected.
 *
 * A deferral is read off the refusal's payload and handled first, before the
 * merge-chat fallback every region kind has: it is the one refusal another
 * check can resolve on its own, so it becomes a state on the row rather than
 * an agent spawned to merge the fact by hand.
 */
async function handleAcceptRefusal(
  row: ProposalRow,
  msg: string,
  payload?: unknown,
  pending?: { text?: string; expectedRevision?: string },
) {
  const deferral = deferralFrom(payload)
  if (deferral) {
    // Checked before the merge chat, which every region kind falls back to on
    // any refusal. A deferral already has a cheaper remedy — one more retry,
    // against the entries the row now names — so spawning an agent to merge by
    // hand would skip past the fix and leave a chat to clean up.
    deferredById.value = {
      ...deferredById.value,
      [row.id]: { ...deferral, ...pending },
    }
    return
  }
  const fallback = descriptorFor(row).fallback
  if (fallback && fallback.when(msg)) {
    await mergeViaChat(row, msg, fallback)
    return
  }
  store.error = msg
}

/** Hand a refused accept to a background chat that can merge it by hand.
 *
 * One function for every kind: the four that existed differed only in the chat
 * title, the toast detail and the prompt, all three of which the descriptor now
 * supplies.
 */
async function mergeViaChat(row: ProposalRow, errorMsg: string, fallback: ProposalMergeFallback) {
  if (chatBusy.value) return
  chatBusy.value = true
  try {
    const chat = await openWorkspaceChatInBackground(
      row.workspace,
      fallback.chatTitle(row),
      resolutionHelper(row.id),
    )
    if (!chat) return
    linkProposal(row.id, chat.chat_id)
    projectStore.sendMessage(chat.chat_id, fallback.prompt(row, errorMsg))
    pushBackgroundToast(chat.chat_id, 'Merging in background', fallback.toastDetail(row))
  } finally {
    chatBusy.value = false
  }
}

/** The workspace a justified re-home row would move into. */
function rehomeTarget(row: ProposalRow): string {
  return (row.rehome?.destination ?? '').split('/')[0]
}

/** Where this note could go: every registered workspace except its own.
 *
 * The tags' candidates are a hint, not the menu. Offering only tag-named
 * candidates meant a row with no tag naming anywhere — which is most of them —
 * had no destination to pick and therefore no way to move at all.
 */
function moveTargets(row: ProposalRow): string[] {
  const own = row.workspace
  const named = row.rehome?.candidates ?? []
  const all = projectStore.workspaceOptions.map(w => w.name)
  const ordered = [...named.filter(n => n !== own), ...all.filter(n => n !== own && !named.includes(n))]
  return [...new Set(ordered)]
}

function doDismiss(row: ProposalRow) {
  void store.act(row.id, 'dismiss')
}

async function openWorkspaceChat(workspace: string, title: string) {
  const target = workspace || projectStore.activeWorkspace
  if (projectStore.activeWorkspace !== target) {
    await projectStore.switchWorkspace(target)
  }
  let project = projectStore.projects.find((p) => p.workspace === target && Boolean(p.is_auto))
  if (!project) project = await projectStore.createProject('General')
  if (!project) return null
  return projectStore.createChat(project.project_id, title)
}

async function openWorkspaceChatInBackground(workspace: string, title: string, helper: ProposalHelper) {
  const target = workspace || projectStore.activeWorkspace
  let project = projectStore.projects.find((p) => p.workspace === target && Boolean(p.is_auto))
  if (!project) {
    const { api } = await import('../lib/api')
    try {
      const created = await api.post<any>('/api/projects', { name: 'General', workspace: target, context: '' })
      if (!projectStore.projects.some((p) => p.project_id === created.project_id)) {
        projectStore.projects.push(created)
      }
      project = created
    } catch {
      return null
    }
  }
  if (!project) return null
  try {
    const { api } = await import('../lib/api')
    const chat = await api.post<any>(`/api/projects/${project.project_id}/chats`, { title, helper })
    if (!projectStore.chats.some((c) => c.chat_id === chat.chat_id)) {
      projectStore.chats.push(chat)
    }
    return chat
  } catch {
    return null
  }
}

function pushBackgroundToast(chatId: string, title: string, body: string) {
  projectStore.pushToast({ chat_id: chatId, title, body })
}

/** Open the proposal itself.
 *
 * A skill row showed its filename and the words "a skill proposal file", which
 * is not enough to decide anything: the whole content of the decision is in the
 * file. The row already carries a vault-relative path, which is what the file
 * viewer takes.
 */
async function view(row: ProposalRow) {
  if (!row.path) return
  await fileViewer.open(row.path)
}

/** The last path segment, which is the only part that differs between rows. */
function pathLeaf(path: string): string {
  return path.split('/').pop() || path
}

/** Accept a skill proposal by building it, in a chat, in its own workspace.
 *
 * A skill proposal has nothing to promote into a region, so the server refuses
 * `accept` for it — but "there is nothing to do" was the wrong reading of what
 * accepting a proposed skill means. Accepting it means implementing it, and that
 * is a chat. The row stays queued until the skill actually exists, then the
 * implementation removes it — so nothing is lost if the work stops halfway.
 */
async function implementSkill(row: ProposalRow) {
  if (chatBusy.value) return
  chatBusy.value = true
  try {
    const chat = await openWorkspaceChatInBackground(row.workspace, `Implement ${row.text}`, resolutionHelper(row.id))
    if (!chat) return
    linkProposal(row.id, chat.chat_id)
    projectStore.sendMessage(chat.chat_id, implementPrompt(row))
    pushBackgroundToast(chat.chat_id, 'Building skill in background', `${row.text} — click to open the chat`)
  } finally {
    chatBusy.value = false
  }
}

function implementPrompt(row: ProposalRow): string {
  return (
    `Implement the skill proposed in \`${row.path}\` (queued in the ${row.workspace} ` +
    'workspace). Work in this chat only; do not delegate this helper task.\n\n' +
    'Read the proposal first and tell me what it wants before writing anything. ' +
    'If it is worth building, create it under this workspace\'s `skills/` directory ' +
    'as a `SKILL.md` with a name and description, then run `ciao sync-skills` for ' +
    'this root so the providers can see it. If it is not worth building, say so and ' +
    'why — a proposal is a suggestion, not an instruction.\n\n' +
    'Once the skill is actually in place (or you have decided not to build it), ' +
    `remove the proposal with \`ciao skill-proposal-remove <name>\` naming ` +
    `\`${row.text}\`, so it stops re-asking in the review queue. If we stop halfway, ` +
    'leave the proposal in place so the decision is not lost.'
  )
}

async function discuss(row: ProposalRow) {
  // The row stays queued: this is "talk about it", not a decision. The chat is
  // created in the row's OWN workspace, because a proposal from work discussed
  // in a personal chat is read against the wrong vault, the wrong guide and the
  // wrong people.
  if (chatBusy.value) return
  chatBusy.value = true
  try {
    const chat = await openWorkspaceChatInBackground(row.workspace, 'Proposal review', reviewHelper(row.id))
    if (!chat) return
    linkDiscussion([row.id], chat.chat_id)
    projectStore.sendMessage(chat.chat_id, discussPrompt(row))
    pushBackgroundToast(chat.chat_id, 'Discussion in background', 'Proposal review — click to open the chat')
  } finally {
    chatBusy.value = false
  }
}

function discussPrompt(row: ProposalRow): string {
  const where = `queued in the ${row.workspace} workspace`
  if (isRehome(row)) {
    return (
      `A person note may be filed in the wrong workspace (${where}): ${row.text}\n\n` +
      'Check the note\'s tags and content, tell me which workspace it belongs to and why, ' +
      'and move it with `ciao vault-rehome` only if the evidence is clear. ' +
      'Leave the proposal queued either way; I will accept or dismiss it myself.'
    )
  }
  const about = descriptorFor(row).discussLabel(row)
  return (
    `${about} is waiting for a decision (${where}): ${row.text}\n\n` +
    'Tell me whether this is durable and cross-session enough to keep, and where it ' +
    'should live — a bounded region, a project doc, a person note, Learnings, or ' +
    'nowhere. Do not edit anything until I decide.\n\n' +
    DISCUSS_RESOLUTION
  )
}

// A discussion is where the decision often gets made, so the chat has to know
// how to carry it out. Without this the agent had to discover the dismissal
// command on its own, and when it could not it told the operator to go back to
// the review page.
const DISCUSS_RESOLUTION =
  'If I tell you to drop it, write its exact text to a file and run ' +
  '`ciao memory-proposal-dismiss --text-file <file>`. If I tell you to keep it, ' +
  'file it where we agreed first, then run the same command with `--promoted`. ' +
  'Never pass the text as a shell argument or edit the queue file by hand. ' +
  'Until I decide, leave the proposal queued.'

function batchAccept() {
  if (!selectedAcceptable.value.length) return
  void store.batch(selectedAcceptable.value, 'accept')
}

/** Workspaces every selected re-home row could move to. */
const batchMoveTargets = computed(() => {
  const rows = selectedVisible.value.filter(isRehome)
  if (!rows.length) return [] as string[]
  const own = new Set(rows.map(r => r.workspace))
  return projectStore.workspaceOptions.map(w => w.name).filter(n => !own.has(n))
})

const selectedRehomeCount = computed(() => selectedVisible.value.filter(isRehome).length)

function batchMove(workspace: string) {
  const ids = selectedVisible.value.filter(isRehome).map(r => r.id)
  if (!ids.length) return
  void store.batch(ids, 'accept', workspace)
}

function batchDismiss() {
  const ids = selectedVisible.value.map(r => r.id)
  if (!ids.length) return
  void store.batch(ids, 'dismiss')
}

async function batchDiscuss() {
  // One chat for the whole selection, in the active workspace, and the rows stay
  // queued. Opening a chat per row would be unusable at the counts this queue
  // reaches, and the rows are usually related — which is why they were selected
  // together.
  const rows = selectedVisible.value
  if (!rows.length || chatBusy.value) return
  chatBusy.value = true
  try {
    const chat = await openWorkspaceChatInBackground(projectStore.activeWorkspace, 'Proposal review', reviewHelper(...rows.map(row => row.id)))
    if (!chat) return
    linkDiscussion(rows.map(row => row.id), chat.chat_id)
    const lines = rows.map((r, i) => `${i + 1}. [${r.kind}] ${r.text}`).join('\n')
    projectStore.sendMessage(
      chat.chat_id,
      `${rows.length} queued proposals need a decision:\n\n${lines}\n\n` +
        'For each one, tell me whether it is durable and belongs where it says, ' +
        'or should be dropped. Do not edit any region or move any file until I ' +
        'decide.\n\n' + DISCUSS_RESOLUTION,
    )
    pushBackgroundToast(chat.chat_id, 'Discussion in background', `${rows.length} proposals — click to open the chat`)
  } finally {
    chatBusy.value = false
  }
}

/** Checkboxes stay out of the way until asked for: most visits decide one
 *  row at a time, and a checkbox on every row read as the primary control. */
const selecting = ref(false)
const menuOpen = ref(false)

// Esc closes the menu and stops there. The layout's window Esc handler goes
// home from Memory unless the press was already handled, and Reka's own Esc
// listener sits on window too, registered later, so it cannot mark the press
// in time. Handling it on the menu element runs first, while it bubbles.
function closeMenuOnEscape(event: KeyboardEvent) {
  event.preventDefault()
  menuOpen.value = false
}
const showChecks = computed(() => selecting.value || selected.value.size > 0)

function stopSelecting() {
  selecting.value = false
  selected.value = new Set()
}

// The bulk clean-up lives in the section's menu, asked as a question rather
// than an always-visible form row under the queue.
async function askDismissOlder() {
  const answer = await askPrompt('Suggestions waiting longer than this are dismissed.', {
    title: 'Dismiss old suggestions',
    value: String(olderThanDays.value),
    placeholder: 'Days',
    confirmLabel: 'Dismiss',
  })
  if (answer === null) return
  const days = Math.round(Number(answer))
  if (!Number.isFinite(days) || days < 1 || days > 365) return
  olderThanDays.value = days
  dismissOlder()
}

function dismissOlder() {
  const date = new Date()
  date.setDate(date.getDate() - olderThanDays.value)
  const iso = date.toISOString().slice(0, 10)
  void store.dismissOlderThan(iso)
}

onMounted(() => {
  pruneProposalChatLinks()
  void store.fetch().then(pruneProposalChatLinks)
  // Prefetch the ledger for the tab badge. Loading it only on the tab switch
  // meant the badge never appeared until the tab had been opened once, which
  // is the one moment it has nothing left to tell you.
  void store.ensureHistoryLoaded(projectStore.activeWorkspace)
})

// Re-scope the ledger when the workspace changes. `ProposalHistoryList` has
// its own watcher, but it is `v-if`-ed out while the Queue tab is showing, so
// switching workspace from there left the History badge reporting the previous
// workspace's total until the tab was opened or a queue mutation refetched.
watch(
  () => projectStore.activeWorkspace,
  ws => { void store.ensureHistoryLoaded(ws) },
)
</script>

<template>
  <div class="proposal-review">

    <ProposalHistoryList v-if="activeSection === 'history'" />

    <div v-else>
    <!-- One sentence naming the decision, and the mechanism behind it folded
         away. The paragraph this replaces ran nine lines — internal routing,
         bounded regions, stub notes, file removal — and at a 390px viewport it
         took about 230px of screen before the first thing to decide. Where a
         row would actually go is now on the row itself. -->
    <p class="pr-lede">
      Facts Ciao was not sure enough to save on its own. Review shows where each one would be saved.
      <button
        v-if="store.kindFilter !== 'all' || store.search"
        type="button"
        class="pr-clear-filter"
        @click="store.resetFilters()"
      >Clear filter</button>
    </p>
    <details class="pr-how">
      <summary class="pr-how-summary">How memory works</summary>
      <div class="pr-how-body">
        <p>
          When you archive a chat, Ciaobot saves what it is confident about by
          itself. Anything it is unsure about waits here instead, so nothing it
          guessed at lands in your notes without you seeing it.
        </p>
        <p>
          Each row says what keeping it would do. A nightly pass looks again at
          this list and at your older notes, so a row can also clear itself once
          that pass can settle it.
        </p>
        <p>
          Notes that are already saved but may have gone out of date are under
          <strong>Notes to revisit</strong>, not here.
        </p>
      </div>
    </details>


    <!-- Counted and gated on the VISIBLE selection, so the bar can never
         advertise (or act on) rows the current workspace/kind filter hides. -->
    <div v-if="selectedVisible.length" class="pr-batch">
      <span class="pr-batch-count">{{ selectedVisible.length }} selected</span>
      <!-- Absent rather than disabled when nothing in the selection can be
           accepted, matching a row's own actions. Rendering "accept 0" invited
           the click that dropped a re-home bullet while moving nothing. -->
      <button
        v-if="selectedAcceptable.length"
        type="button"
        class="btn-small btn-primary"
        :disabled="store.busy"
        @click="batchAccept"
      >Accept {{ selectedAcceptable.length }}</button>
      <!-- One destination for the whole selection. Re-home rows are moves, so
           "accept" cannot cover them: a move needs somewhere to go. -->
      <button
        v-for="target in batchMoveTargets"
        :key="`move-${target}`"
        type="button"
        class="btn-small btn-primary"
        :disabled="store.busy"
        @click="batchMove(target)"
      >Move {{ selectedRehomeCount }} to {{ target }}</button>
      <button
        type="button"
        class="btn-small btn-chip"
        :disabled="store.busy"
        @click="batchDismiss"
      >Dismiss {{ selectedVisible.length }}</button>
      <button
        type="button"
        class="btn-small btn-chip"
        :disabled="chatBusy"
        @click="batchDiscuss"
      >Talk about {{ selectedVisible.length }}</button>
      <button type="button" class="btn-small btn-chip" @click="selected = new Set()">Clear</button>
    </div>

    <!-- What the last bulk action did, per destination. Fifty per-row lines
         repeat the queue the operator was just looking at and never answer the
         question a bulk accept raises, which is what changed and where. The
         per-row failures are not averaged away: each group counts them and
         the rows that failed are still queued below. -->
    <div v-if="store.lastBatchSummary.length" class="pr-summary-block" role="status" aria-live="polite">
      <div class="pr-summary-head">
        <span class="pr-summary-title">Last {{ store.lastBatchSummary[0].action }}</span>
        <button type="button" class="btn-small btn-chip" @click="store.lastBatchSummary = []">Dismiss</button>
      </div>
      <ul class="pr-summary-rows">
        <li v-for="group in store.lastBatchSummary" :key="group.destination || 'none'" class="pr-summary-row">
          <span class="pr-summary-dest">{{ group.destination || 'no destination' }}</span>
          <span class="pr-summary-counts">
            {{ group.ok }} of {{ group.total }} applied<template v-if="group.duplicates">, {{ group.duplicates }} already known</template><template v-if="group.conflicts">, {{ group.conflicts }} changed underneath</template><template v-if="group.failed">, {{ group.failed }} still queued</template>
          </span>
          <span v-if="group.errors.length" class="pr-summary-error">{{ group.errors[0] }}</span>
        </li>
      </ul>
    </div>

    <!-- The queue's four load states, kept apart so none of them can borrow the
         others' words. The old single "Nothing queued here." rendered under a
         slow or failed first GET and read as a confirmed-empty queue. -->
    <p v-if="queueLoading" class="pr-empty" role="status" aria-live="polite">Loading proposals…</p>

    <div v-else-if="queueFailed" class="pr-error-block" role="alert">
      <p class="pr-error">{{ store.loadError }}</p>
      <button type="button" class="btn-small btn-chip" @click="retryQueue">Retry</button>
    </div>

    <!-- A refresh failed while rows are already on screen: keep showing them,
         but say they are the last snapshot rather than the current one. -->
    <div v-else-if="store.loadError" class="pr-stale" role="status">
      <span>Could not refresh — showing the last loaded queue.</span>
      <button type="button" class="btn-small btn-chip" @click="retryQueue">Retry</button>
    </div>

    <!-- Empty-state claims render only on a successful load with no failed
         refresh shadowing it. `filtersHideEverything` needs a filter to be the
         reason; `queueEmpty` is the only branch allowed to say "All reviewed." -->
    <template v-if="queueSettled && !store.loadError">
      <p v-if="filtersHideEverything" class="pr-empty">
        No proposals match the current filters.
        <button
          v-if="store.kindFilter !== 'all' || store.search"
          type="button"
          class="pr-clear-filter"
          @click="store.resetFilters()"
        >Clear filters</button>
      </p>
      <p v-else-if="queueEmpty" class="pr-empty">All reviewed.</p>
    </template>

    <template v-if="queueSettled">
    <section class="pr-group">
      <!-- Count on the left, section actions on the right: Select turns the
           row checkboxes on, the menu holds the rare bulk clean-up. -->
      <header v-if="filtered.length" class="pr-group-head">
        <label v-if="showChecks" class="pr-group-select">
          <input type="checkbox" :checked="allSelected" @change="toggleAll" />
          <span class="pr-group-name">Select all {{ filtered.length }}</span>
        </label>
        <span v-else class="pr-group-name">{{ filtered.length === 1 ? '1 suggestion' : `${filtered.length} suggestions` }}</span>
        <span class="pr-group-tools">
          <button
            v-if="showChecks"
            type="button"
            class="pr-text-btn pr-select-toggle"
            @click="stopSelecting"
          >Done</button>
          <button
            v-else
            type="button"
            class="pr-text-btn pr-select-toggle"
            @click="selecting = true"
          >Select</button>
          <DropdownMenuRoot v-model:open="menuOpen" :modal="false">
            <DropdownMenuTrigger as-child>
              <button type="button" class="btn-icon pr-more" aria-label="More suggestion actions" title="More">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg>
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent as-child align="end" :side-offset="6" :collision-padding="8">
              <div class="pr-menu" @keydown.esc="closeMenuOnEscape">
                <DropdownMenuItem as-child :disabled="store.busy" @select="askDismissOlder">
                  <button type="button" class="pr-dismiss-older">Dismiss suggestions older than…</button>
                </DropdownMenuItem>
              </div>
            </DropdownMenuContent>
          </DropdownMenuRoot>
        </span>
      </header>

      <template v-for="group in groups" :key="group.key">
        <!-- Legacy dated proposals remain one decision until the next reflection
             upserts their canonical file. -->
        <header v-if="group.label" class="pr-group-label">
          <span class="pr-group-label-name">{{ group.label }}</span>
          <span class="pr-group-label-count">{{ group.rows.length }}</span>
        </header>
        <ul class="pr-rows" :class="{ 'pr-rows--plain': !showChecks }">
        <li
          v-for="row in group.rows"
          :key="row.id"
          class="pr-row"
          :class="{ 'pr-row--leak': row.leak_warning, 'pr-row--busy': store.isBusy(row.id), 'pr-row--linked': hasActiveLink(row) }"
        >
          <!-- Wrapped so the tap target reaches 44px; the input itself keeps its
               native size, and the aria-label names the fact this row controls. -->
          <label v-if="showChecks" class="pr-row-check-hit">
            <input
              class="pr-row-check"
              type="checkbox"
              :checked="selected.has(row.id)"
              :aria-label="`Select ${kindLabel(row.kind)}: ${rowTitle(row)}`"
              @change="toggleRow(row.id)"
            />
          </label>

          <div class="pr-row-body">
            <!-- Kind and source first, as one quiet line, so the fact itself
                 starts at the row's left edge and reads as the subject. -->
            <p class="pr-row-meta">
              <span class="pr-kind" :class="`pr-kind--${row.kind}`">{{ kindLabel(row.kind) }}</span>
              <template v-if="row.source && !isRehome(row) && !isSkill(row)"> · from {{ row.source }}</template>
            </p>
            <div class="pr-row-top">
              <!-- Backtick spans render as inline code rather than raw backticks;
                   built from segments, never v-html, since the text is model-written. -->
              <span class="pr-row-title"><template v-for="(part, pi) in inlineCodeParts(rowTitle(row))" :key="pi"><code v-if="part.code" class="pr-inline-code">{{ part.text }}</code><template v-else>{{ part.text }}</template></template></span>
            </div>
            <!-- What accepting this row would do, in words rather than a path:
                 `ciao:memory` and `Workspace/Learnings.md` are the same shape of
                 string and say nothing about the difference between them. The
                 path is still one disclosure away, and still the title text.

                 For a skill row the file IS the row, so its leaf stays a button
                 that opens it — a separate "view" button spent a slot saying
                 what the path already said. -->
            <p class="pr-row-sub" :title="rowSubtitle(row)">
              <button
                v-if="isSkill(row) && row.path"
                type="button"
                class="pr-path-link"
                :title="row.path"
                @click="view(row)"
              >{{ pathLeaf(row.path) }}</button>
              <template v-else>{{ rowConsequence(row) }}</template>
              <span v-if="row.leak_warning" class="pr-badge --warn">visible in every workspace</span>
            </p>
            <details class="pr-row-detail">
              <summary>Details</summary>
              <p v-if="rowDetail(row)" class="pr-row-prose">{{ rowDetail(row) }}</p>
              <p class="pr-row-source">Goes to {{ rowSubtitle(row) }}</p>
              <p v-if="row.path" class="pr-row-source">{{ row.path }}</p>
            </details>
          </div>

          <!-- The decision card. It replaces the row's actions until answered,
               and it is the ONLY place an accept is confirmed from: the bullet's
               text is not the change, so a card that names the destination and
               shows the server's exact replacement is what makes the accept a
               decision rather than a guess. One primary action; edit, discuss
               and dismiss are secondary. The leak warning lives here now too,
               rather than as a second confirmation stacked on top of this one. -->
          <div v-if="isPreviewOpen(row)" class="pr-card" role="group" :aria-label="`Review ${rowTitle(row)}`">
            <p v-if="store.isPreviewLoading(row.id) && !store.previews[row.id]" class="pr-card-note" role="status">Reading the destination…</p>
            <p v-else-if="store.previewErrors[row.id]" class="pr-card-error" role="alert">{{ store.previewErrors[row.id] }}</p>

            <template v-else-if="store.previews[row.id]">
              <p v-if="store.conflictIds.has(row.id)" class="pr-card-conflict" role="alert">
                The destination changed since this preview, so nothing was written.
                This is what it would do now.
              </p>

              <div class="pr-card-head">
                <span class="pr-card-op" :class="`pr-card-op--${store.previews[row.id].operation || 'none'}`">
                  {{ operationLabel(store.previews[row.id].operation) }}
                </span>
                <span class="pr-card-dest" :title="store.previews[row.id].destination_path">
                  {{ store.previews[row.id].destination || 'no destination' }}
                </span>
              </div>

              <p class="pr-card-meta">
                <span v-if="row.source">from <span class="pr-card-source">{{ row.source }}</span></span>
                <span v-else>no recorded source</span>
                <span v-if="store.previews[row.id].leak_warning" class="pr-badge --warn">visible in every workspace</span>
              </p>

              <!-- Edit suggestion: a secondary action, and the edited wording is
                   re-previewed against the same destination so what is on screen
                   is always what the accept would write. -->
              <div v-if="editingPreview" class="pr-card-edit">
                <label class="pr-card-edit-label" :for="`pr-edit-${row.id}`">Edit the wording</label>
                <textarea
                  :id="`pr-edit-${row.id}`"
                  v-model="editBuffer"
                  class="pr-card-edit-input"
                  rows="3"
                ></textarea>
                <div class="pr-card-edit-actions">
                  <button type="button" class="btn-small btn-primary" :disabled="!editBuffer.trim() || store.isPreviewLoading(row.id)" @click="applyPreviewEdit">Preview change</button>
                  <button type="button" class="btn-small btn-chip" @click="cancelPreviewEdit">Cancel edit</button>
                </div>
              </div>
              <p v-else class="pr-card-text">{{ store.previews[row.id].text }}</p>

              <!-- The exact replacement, as lines rather than two full bodies:
                   the region is reprinted in full otherwise and the one line that
                   changes is lost in it. -->
              <div v-if="store.previews[row.id].exact && previewChanges(store.previews[row.id]).length" class="pr-card-diff">
                <p class="pr-card-diff-label">What changes in {{ store.previews[row.id].destination }}</p>
                <ul class="pr-card-diff-lines">
                  <li
                    v-for="(change, index) in previewChanges(store.previews[row.id])"
                    :key="`${change.op}-${index}`"
                    class="pr-card-diff-line"
                    :class="`pr-card-diff-line--${change.op}`"
                  >
                    <span class="pr-card-diff-sign" aria-hidden="true">{{ change.op === 'added' ? '+' : '−' }}</span>
                    <span class="pr-card-diff-text">{{ change.text }}</span>
                    <span class="pr-sr-only">{{ change.op }}</span>
                  </li>
                </ul>
                <p v-if="store.previews[row.id].truncated" class="pr-card-note">The destination is too large to show in full.</p>
              </div>
              <p v-else-if="!store.previews[row.id].exact" class="pr-card-note">
                The exact wording is decided when you accept, so it cannot be shown here.
              </p>
              <p v-else class="pr-card-note">Nothing in {{ store.previews[row.id].destination }} changes.</p>

              <p v-if="store.previews[row.id].reason" class="pr-card-reason">{{ store.previews[row.id].reason }}</p>
              <!-- No edited-wording caveat here any more. The ledger still
                   records the ORIGINAL bullet (that is what the nightly curator
                   compares a re-extracted fact against), but the decision row
                   now also carries the id of the receipt that performed the
                   write, so an edited accept reaches History with its real
                   before/after and a working Undo like any other. -->

              <div class="pr-actions pr-actions--card">
                <button
                  v-if="store.previews[row.id].can_accept"
                  type="button"
                  class="btn-small btn-primary"
                  :disabled="store.isBusy(row.id) || store.isPreviewLoading(row.id)"
                  @click="confirmPreview(row)"
                >{{ store.isBusy(row.id) ? 'working…' : previewPrimaryLabel(row) }}</button>
                <!-- The same write, with one check in front of it: a fact that
                     replaces something already remembered is merged over it
                     instead of added beside it. A chip, not a second primary —
                     the plain save remains the routine decision, and this one
                     costs a model call, which is why it is asked for rather
                     than always done. The card's replacement is what a plain
                     save would write; a check can land on a different line,
                     which is what the title says. -->
                <button
                  v-if="store.previews[row.id].can_accept && canReconcile(row)"
                  type="button"
                  class="btn-small btn-chip"
                  title="Compare this with what is already remembered before writing it, so a fact it replaces is updated instead of duplicated. Takes a few seconds."
                  :disabled="store.isBusy(row.id) || store.isPreviewLoading(row.id)"
                  @click="reconcileFirst(row)"
                >{{ store.isBusy(row.id) ? 'working…' : 'Check first' }}</button>
                <button v-if="!editingPreview" type="button" class="btn-small btn-chip" @click="startEditingPreview">Edit suggestion</button>
                <button v-if="discussionChat(row)" type="button" class="btn-small btn-chip pr-talk" @click="openDiscussion(row)">Open chat</button>
                <button v-else type="button" class="btn-small btn-chip pr-talk" :disabled="chatBusy" @click="discuss(row)">Talk about it</button>
                <button type="button" class="btn-small btn-chip" :disabled="store.isBusy(row.id)" @click="doDismiss(row)">Dismiss</button>
                <button type="button" class="btn-small btn-chip" @click="closePreview">Cancel</button>
              </div>
            </template>
          </div>

          <!-- Deferred: the last accept reconciled this fact against the region
               and could not tell whether it supersedes something already there,
               so nothing was written and the row is still queued. Shown in place
               of the row's actions, and of the decision card that raised it,
               because the next step is not "save or dismiss" but "decide about
               these entries": the
               reason, what it was weighed against, and one more attempt against
               the region as it stands now. -->
          <div v-else-if="deferredFor(row)" class="pr-actions pr-actions--deferred">
            <p class="pr-deferred-reason">Nothing was written: {{ deferredFor(row)!.reason }}</p>
            <template v-if="deferredFor(row)!.competing.length">
              <p class="pr-deferred-label">Weighed against</p>
              <ul class="pr-deferred-competing">
                <li v-for="entry in deferredFor(row)!.competing" :key="entry">{{ entry }}</li>
              </ul>
            </template>
            <button
              type="button"
              class="btn-small btn-primary"
              :disabled="store.isBusy(row.id)"
              @click="retryReconcile(row)"
            >{{ store.isBusy(row.id) ? 'checking…' : 'Try again' }}</button>
            <button type="button" class="btn-small btn-chip" @click="clearDeferred(row.id)">Leave it queued</button>
          </div>

          <!-- Linked: this proposal already spawned a merge/implement chat that
               is still active. The row stays queued while the agent works, so
               replace the accept/dismiss buttons with a link to that chat.
               If the chat was closed/archived without removing the row, the
               watcher clears the link and this collapses back to the normal
               actions. -->
          <div v-else-if="hasActiveLink(row)" class="pr-actions pr-actions--linked">
            <span class="pr-linked-label">Working in <strong>{{ linkedChatTitle(row) }}</strong></span>
            <button type="button" class="btn-small btn-chip pr-row-action" @click="openLinkedChat(row)">Open chat</button>
            <button type="button" class="btn-small btn-chip" @click="clearLink(row.id)">Show actions</button>
          </div>

          <!-- Any rehome row that is not a plain justified accept: pick the
               destination, never pre-filled. Every registered workspace, not
               just tag-named candidates — most rows have no tag naming anywhere,
               and offering them nothing to pick is why they could not be moved. -->
          <div v-else-if="isRehome(row) && rehomeMode(row) !== 'accept'" class="pr-actions">
            <span class="pr-confirm-text">Move to…</span>
            <button
              v-for="c in moveTargets(row)"
              :key="c"
              type="button"
              class="btn-small btn-chip pr-row-action"
              :disabled="store.isBusy(row.id)"
              @click="doAccept(row, c)"
            >{{ store.isBusy(row.id) ? 'working…' : c }}</button>
            <button type="button" class="btn-small btn-chip pr-quiet" :disabled="store.isBusy(row.id)" @click="doDismiss(row)">Dismiss</button>
            <button v-if="discussionChat(row)" type="button" class="btn-small btn-chip pr-talk" @click="openDiscussion(row)">Open chat</button>
            <button v-else type="button" class="btn-small btn-chip pr-talk" :disabled="chatBusy" @click="discuss(row)">Talk about it</button>
          </div>

          <!-- A skill proposal is a FILE, so its actions are the ones a file
               has: read it, build it, or drop it. "Accept" for a region row means
               "write this fact"; for a proposed skill it means "implement it",
               which is a chat, not a write. -->
          <div v-else-if="isSkill(row)" class="pr-actions">
            <button
              type="button"
              class="btn-small btn-chip pr-row-action"
              :disabled="chatBusy"
              @click="implementSkill(row)"
            >Implement</button>
            <button type="button" class="btn-small btn-chip pr-quiet" :disabled="store.isBusy(row.id)" @click="doDismiss(row)">{{ store.isBusy(row.id) ? 'working…' : 'Dismiss' }}</button>
            <button v-if="discussionChat(row)" type="button" class="btn-small btn-chip pr-talk" @click="openDiscussion(row)">Open chat</button>
            <button v-else type="button" class="btn-small btn-chip pr-talk" :disabled="chatBusy" @click="discuss(row)">Talk about it</button>
          </div>

          <div v-else class="pr-actions">
            <!-- No accept when nothing backs a destination: a button that cannot
                 do what it says is worse than absent. A justified re-home names
                 the destination on the button, because "accept" does not say that
                 a file is about to move. -->
            <button
              v-if="canAccept(row)"
              type="button"
              class="btn-small btn-chip pr-row-action"
              :disabled="store.isBusy(row.id)"
              @click="reviewAccept(row)"
            >{{ store.isBusy(row.id) ? 'working…' : (isRehome(row) ? `Move to ${rehomeTarget(row)}` : 'Review') }}</button>
            <button type="button" class="btn-small btn-chip pr-quiet" :disabled="store.isBusy(row.id)" @click="doDismiss(row)">{{ store.isBusy(row.id) ? 'working…' : 'Dismiss' }}</button>
            <button v-if="discussionChat(row)" type="button" class="btn-small btn-chip pr-talk" @click="openDiscussion(row)">Open chat</button>
            <button v-else type="button" class="btn-small btn-chip pr-talk" :disabled="chatBusy" @click="discuss(row)">Talk about it</button>
          </div>
        </li>
        </ul>
      </template>
    </section>

    </template>
    </div>
  </div>
</template>

<style scoped>
/* One column, generous vertical rhythm, and every row the same shape. The old
   layout stacked three unrelated control rows above a list whose items were
   paragraphs, so nothing had a predictable position. */
/* The panel sits in the Memory page's main column, which scrolls; it no
   longer owns a scroller or its own inset. */
.proposal-review {
  min-width: 0;
  padding-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.pr-counts {
  display: inline-flex;
  gap: var(--space-2);
  margin-left: var(--space-2);
  color: var(--fg2);
  font-size: 0.8rem;
}

/* The one sentence that says what this list is. Full-contrast and at body
   size, because it is the first thing read — the nine-line muted paragraph it
   replaces was both harder to read and longer than the screen it opened on. */
.pr-lede {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
  max-width: 72ch;
}

/* The mechanism, folded away. Closed it costs one line; the summary is a real
   disclosure control, so it is keyboard-reachable and states its own state. */
.pr-how {
  margin: calc(-1 * var(--space-3)) 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
}

.pr-how-summary {
  display: inline-flex;
  align-items: center;
  min-height: var(--touch);
  color: var(--accent);
  cursor: pointer;
}

.pr-how-summary:hover { text-decoration: underline; text-underline-offset: 3px; }
.pr-how-summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.pr-how-body {
  max-width: 62ch;
  line-height: 1.5;
}

.pr-how-body p {
  margin: 0 0 var(--space-2);
}

.pr-how-body p:last-child { margin-bottom: 0; }

.pr-error {
  color: var(--error);
  font-size: 0.85rem;
}

.pr-empty {
  color: var(--fg2);
  font-size: 0.9rem;
  padding: var(--space-4) 0;
}

/* A failed first load: the error stands alone with a retry, and no empty-queue
   claim sits under it. */
.pr-error-block {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  padding: var(--space-3) 0;
}

.pr-error-block .pr-error {
  margin: 0;
}

/* A refresh that failed while rows are on screen. Muted, not an alert: the
   data is still usable, it is just the last snapshot. */
.pr-stale {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  color: var(--warning);
  font-size: 0.85rem;
}

/* The batch bar appears only with a selection, so it never occupies space while
   reading. It is sticky, so it must be OPAQUE and above the rows: it used to
   name a token this app does not define and fall back to a 4%-white wash, which
   the row it covered stayed legible through and looked like a rendering fault. */
.pr-batch {
  position: sticky;
  top: 0;
  z-index: 3;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.28);
}

/* Stacked, so the text column gets the width. Four buttons in a row squeezed a
   long skill name into six wrapped lines beside a mostly-empty action strip. */
/* Actions sit on their own line under the fact, left-aligned with it: one
   primary, a neutral secondary, and "talk about it" as a link. */
.pr-actions {
  grid-column: 2;
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.pr-actions :deep(.btn-small),
.pr-card :deep(.btn-small) {
  min-height: 34px;
  padding: 0 12px;
  border-radius: 8px;
  font-size: var(--text-sm);
  font-weight: 600;
}

.pr-actions .btn-chip,
.pr-card .btn-chip {
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--fg);
}

.pr-actions .btn-chip:hover:not(:disabled),
.pr-card .btn-chip:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.pr-actions .pr-talk,
.pr-card .pr-talk {
  border-color: transparent;
  background: none;
  color: var(--accent);
  font-weight: 500;
}

.pr-actions .pr-talk:hover:not(:disabled),
.pr-card .pr-talk:hover:not(:disabled) {
  border-color: transparent;
  text-decoration: underline;
  text-underline-offset: 3px;
}

/* A dismissal is routine and reversible, so it reads as a quiet text action
   beside the row's one neutral button, not a second bordered one. */
.pr-actions .pr-quiet {
  border-color: transparent;
  background: none;
  color: var(--fg2);
  font-weight: 500;
}
.pr-actions .pr-quiet:hover:not(:disabled) {
  border-color: transparent;
  color: var(--fg);
}

@media (pointer: coarse) {
  .pr-actions :deep(.btn-small),
  .pr-card :deep(.btn-small) { min-height: var(--touch); }
}

.pr-actions--confirm {
  min-width: 0;
}

/* Wider than the other action columns because it carries prose and a list of
   region entries, not just buttons. It still collapses to the full row width
   under 640px, where `.pr-actions` spans the grid. */
.pr-actions--deferred {
  flex-direction: column;
  align-items: flex-start;
  max-width: 60ch;
}

.pr-deferred-reason {
  margin: 0;
  font-size: 0.8rem;
  line-height: 1.5;
  color: var(--warning);
  overflow-wrap: anywhere;
}

.pr-deferred-label {
  margin: var(--space-1) 0 0;
  font-size: 0.72rem;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--fg3);
}

.pr-deferred-competing {
  margin: 0;
  padding-left: var(--space-3);
  font-size: 0.78rem;
  line-height: 1.5;
  color: var(--fg2);
  overflow-wrap: anywhere;
}

/* ── Decision card ────────────────────────────────────────────────────────
   The row is a three-column grid (checkbox | body | actions). The card is a
   decision, not an action strip, so it takes a full-width row of its own
   underneath rather than being squeezed into the action column — a diff line
   wrapped to one word per line in 8.5rem. */
.pr-card {
  grid-column: 2;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--border-strong, var(--border));
  border-radius: var(--radius-sm);
  background: var(--bg3, var(--bg2));
}

.pr-card-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}

/* Shape and text, never colour alone: the operation has to read the same in a
   monochrome or high-contrast rendering. */
.pr-card-op {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0.1rem 0.45rem;
  border: 1px solid var(--border-strong, var(--border));
  border-radius: 4px;
  color: var(--fg);
}

.pr-card-op--add {
  border-color: var(--success);
}

.pr-card-op--update,
.pr-card-op--move {
  border-color: var(--accent);
}

.pr-card-op--none {
  color: var(--fg2);
}

.pr-card-dest {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.8rem;
  color: var(--fg);
  overflow-wrap: anywhere;
}

.pr-card-meta {
  margin: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  color: var(--fg2);
  font-size: 0.78rem;
}

.pr-card-source {
  font-family: var(--font-mono, ui-monospace, monospace);
  overflow-wrap: anywhere;
}

.pr-card-text {
  margin: 0;
  font-size: 0.9rem;
}

.pr-card-edit {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.pr-card-edit-label {
  font-size: 0.78rem;
  color: var(--fg2);
}

.pr-card-edit-input {
  width: 100%;
  box-sizing: border-box;
  /* 16px: anything smaller makes iOS Safari zoom the whole page on focus. */
  font-size: 1rem;
  font-family: inherit;
  line-height: 1.5;
  padding: var(--space-2);
  color: var(--fg);
  background: var(--bg);
  border: 1px solid var(--border-strong, var(--border));
  border-radius: var(--radius-sm);
  resize: vertical;
}

.pr-card-edit-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.pr-card-diff {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.pr-card-diff-label {
  margin: 0;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--fg2);
}

.pr-card-diff-lines {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pr-card-diff-line {
  display: flex;
  gap: var(--space-2);
  padding: 0.2rem var(--space-2);
  border-left: 3px solid var(--border);
  border-radius: 3px;
  background: var(--bg);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.78rem;
  line-height: 1.5;
}

.pr-card-diff-line--added {
  border-left-color: var(--success);
}

.pr-card-diff-line--removed {
  border-left-color: var(--error);
  text-decoration: line-through;
  color: var(--fg2);
}

.pr-card-diff-sign {
  flex: none;
  opacity: 0.8;
}

.pr-card-diff-text {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.pr-card-note,
.pr-card-reason {
  margin: 0;
  color: var(--fg2);
  font-size: 0.8rem;
}

.pr-card-error {
  margin: 0;
  color: var(--error);
  font-size: 0.82rem;
}

.pr-card-conflict {
  margin: 0;
  padding: var(--space-2);
  border: 1px solid var(--warning);
  border-radius: var(--radius-sm);
  color: var(--fg);
  font-size: 0.82rem;
}

/* The card's actions run across, not down: the column layout above exists to
   leave the text room, and here the card already owns the full width. */
.pr-actions--card {
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  min-width: 0;
}

/* Six controls at phone width — one primary plus five neutral chips, after
   the reconcile check joined the card. Nothing overflows and every target is
   still 44px, but at 320px the default 16px side padding pushes the strip
   into five wrapped rows: 236px of buttons, a third of the screen, under a
   card that also has to show the destination and the replacement. Tightening
   only the horizontal padding packs it back to four rows (188px) at 320px and
   keeps three at 390px, with the 44px hit area untouched — it comes from
   `min-height`, not from this padding. The chips stay neutral: crowding is
   not a reason to promote a secondary into a second pink bar. */
@media (max-width: 480px) {
  .pr-actions--card .btn-small,
  .pr-card-edit-actions .btn-small {
    padding-left: var(--space-3);
    padding-right: var(--space-3);
  }
}

.pr-summary-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
}

.pr-summary-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.pr-summary-title {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--fg2);
}

.pr-summary-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pr-summary-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  font-size: 0.82rem;
}

.pr-summary-dest {
  font-family: var(--font-mono, ui-monospace, monospace);
  overflow-wrap: anywhere;
}

.pr-summary-counts {
  color: var(--fg2);
}

.pr-summary-error {
  width: 100%;
  color: var(--error);
  font-size: 0.78rem;
}

.pr-sr-only {
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

.pr-group-label {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin: var(--space-3) 0 var(--space-1);
}

.pr-group-label-name {
  font-weight: 600;
  color: var(--fg);
}

.pr-group-label-count {
  font-size: 0.75rem;
  color: var(--fg3);
  font-variant-numeric: tabular-nums;
}

/* The path is the button: a skill row's whole content is the file it names. */
.pr-path-link {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  font-family: var(--font-mono);
  font-size: 0.78rem;
  color: var(--accent);
  text-align: left;
  cursor: pointer;
  overflow-wrap: anywhere;
}

.pr-path-link:hover {
  text-decoration: underline;
}

.pr-clear-filter {
  margin-left: var(--space-1);
  background: none;
  border: none;
  padding: 0;
  color: var(--accent);
  font-size: var(--text-sm);
  cursor: pointer;
}

/* Touch: the inline link is only glyph-height, well under the 44px minimum.
   Grow the hit area with padding and pull the extra back with a matching
   negative margin, so the control stays visually inline where it sits in the
   empty-state sentence. Same trick as the History list's clear control. */
@media (pointer: coarse) {
  .pr-clear-filter {
    --pr-clear-visual: 1.1rem;
    --pr-clear-pad: calc((var(--touch, 44px) - var(--pr-clear-visual)) / 2);
    display: inline-flex;
    align-items: center;
    min-height: var(--touch, 44px);
    padding: var(--pr-clear-pad);
    margin: calc(-1 * var(--pr-clear-pad));
    margin-left: calc(var(--space-2) - var(--pr-clear-pad));
  }

  /* The card is where the decision is made, so its controls get the full touch
     target rather than the panel's compact chip height. */
  .pr-actions--card .btn-small,
  .pr-card-edit-actions .btn-small {
    min-height: var(--touch, 44px);
  }
}

.pr-batch-count {
  font-size: 0.8rem;
  color: var(--fg2);
  margin-right: auto;
}

/* The scope bar: a select-all and the count for the workspace the sidebar has
   selected. The workspace itself lives in the left switcher, like every other
   page, rather than in a heading you have to scroll back to. */
.pr-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.pr-group-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 36px;
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-size: var(--text-sm);
}

.pr-group-select {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
}

.pr-group-name {
  font-weight: 500;
}

.pr-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}

/* Hairline rows, not cards: the checkbox, then the fact and its source, with
   the actions on their own line beneath. */
.pr-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: var(--space-2) var(--space-2);
  padding: var(--space-3) 0 var(--space-4);
  border-bottom: 1px solid var(--border);
}

.pr-row--leak {
  background: color-mix(in srgb, var(--warning) 6%, transparent);
}

.pr-row--busy {
  opacity: 0.72;
  pointer-events: none;
}

.pr-row--busy .pr-row-top,
.pr-row--busy .pr-row-sub {
  opacity: 0.6;
}

.pr-row--linked {
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}

.pr-actions--linked {
  min-width: 10rem;
}

.pr-linked-label {
  font-size: 0.78rem;
  color: var(--fg2);
  line-height: 1.4;
}

.pr-linked-label strong {
  color: var(--fg);
  font-weight: 600;
}

/* Full-height tap target around the native checkbox (DESIGN.md's --touch),
   so a thumb on a phone can hit the row's selection control. */
.pr-row-check-hit {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  min-width: 32px;
  min-height: 32px;
  padding-top: 0.25rem;
  cursor: pointer;
}

@media (pointer: coarse) {
  .pr-row-check-hit { min-width: var(--touch); min-height: var(--touch); }
}

.pr-row-check {
  margin: 0;
}

.pr-row-body {
  min-width: 0;
}

.pr-row-top {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
}

.pr-row-meta {
  margin: 0 0 3px;
  color: var(--fg3);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

/* The fact is prose to read, not a heading: regular weight at a readable
   measure. Bold paragraphs were what made long suggestions hard to scan. */
.pr-row-title {
  max-width: 72ch;
  color: var(--fg);
  font-size: calc(15px * var(--font-scale));
  font-weight: 450;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.pr-inline-code {
  padding: 0 4px;
  border-radius: var(--radius-xs);
  background: var(--bg3);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 0.86em;
}

.pr-row-sub {
  margin: 4px 0 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

/* The kind is a quiet word before the fact, not a boxed mono tag. */
.pr-kind {
  color: var(--fg2);
  font-weight: 600;
  text-transform: capitalize;
}

.pr-badge {
  margin-left: var(--space-2);
  font-size: 0.7rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
}

.pr-badge.--warn {
  background: rgba(210, 153, 34, 0.18);
  color: var(--warning);
}

/* The original bullet is a paragraph of prose with a CLI incantation in it.
   Useful, but not at the top of every row. */
.pr-row-detail {
  margin-top: 2px;
  font-size: var(--text-sm);
  color: var(--fg3);
}

.pr-row-detail summary {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  cursor: pointer;
}

.pr-row-detail summary:hover { color: var(--fg); }

.pr-row-prose,
.pr-row-source {
  margin: var(--space-2) 0 0;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.pr-row-source {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  opacity: 0.75;
}

.pr-confirm-text {
  font-size: 0.8rem;
  color: var(--fg2);
}

/* Section tools on the count row: text actions and the overflow menu. */
.pr-group-tools {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}
.pr-text-btn {
  display: inline-flex;
  align-items: center;
  min-height: 30px;
  padding: 0;
  border: none;
  background: none;
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-sm);
  cursor: pointer;
}
.pr-text-btn:hover { color: var(--fg); }
.pr-menu {
  z-index: 50;
  min-width: 240px;
  padding: 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 12px 32px rgb(0 0 0 / 28%);
}
.pr-menu button {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 36px;
  padding: 0 12px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
}
.pr-menu button:hover,
.pr-menu button[data-highlighted] { background: var(--bg3); outline: none; }
.pr-menu button[data-disabled] { opacity: 0.45; cursor: default; }
@media (pointer: coarse) {
  .pr-text-btn { min-height: var(--touch); }
  .pr-menu button { min-height: var(--touch); }
}

/* Without checkboxes the row is one column; the body and actions that were
   placed in column 2 move to column 1. */
.pr-rows--plain > .pr-row { grid-template-columns: minmax(0, 1fr); }
.pr-rows--plain > .pr-row > * { grid-column: 1 / -1; }

/* On a phone the actions take the row's full width under the checkbox. */
@media (max-width: 640px) {
  .pr-actions,
  .pr-card {
    grid-column: 1 / -1;
  }
}
</style>
