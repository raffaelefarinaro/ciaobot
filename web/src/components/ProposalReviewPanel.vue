<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useProposalsStore } from '../stores/proposals'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import type { ProposalRow } from '../lib/types'
import { descriptorFor, kindLabel, rehomeMode } from '../lib/proposalKinds'
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

const confirmLeakId = ref('')
const olderThanDays = ref(30)

// Proposal → chat link: when an accept fallback or skill implement spawns a
// chat, the row stays queued while the agent works. Remembering that chat
// lets the row show "open chat" instead of the same accept/dismiss buttons
// until the chat is archived/deleted or the proposal disappears, at which
// point we revert to the normal actions.
const PROPOSAL_CHAT_KEY = 'ciao:proposal-chat-links'

function loadProposalChatLinks(): Record<string, string> {
  try {
    const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(PROPOSAL_CHAT_KEY) : null
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
  // Mirror InAppToast's workspace hop: the chat lives in its own workspace,
  // which may not be the one the review list is currently scoped to.
  const project = projectStore.projectFor(chatId)
  if (project && project.workspace !== projectStore.activeWorkspace) {
    await projectStore.switchWorkspace(project.workspace)
  }
  await projectStore.switchChat(chatId)
}

function pruneProposalChatLinks() {
  const liveIds = new Set(store.rows.map(r => r.id))
  const next = { ...proposalChatLinks.value }
  let changed = false
  for (const pid of Object.keys(next)) {
    if (!liveIds.has(pid)) {
      delete next[pid]
      changed = true
      continue
    }
    const chat = projectStore.chats.find(c => c.chat_id === next[pid])
    if (!chat || chat.archived) {
      delete next[pid]
      changed = true
    }
  }
  if (changed) proposalChatLinks.value = next
}

watch(() => store.rows.map(r => r.id).join(','), pruneProposalChatLinks)
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

// -- Queue / History tabs ---------------------------------------------------
//
// The two sub-views share this panel (and its workspace/kind/search filter
// state in the store) rather than living on separate routes: switching is a
// glance, not a navigation, and the sidebar's scope picker must not reset.
const REVIEW_TABS = [
  { key: 'queue' as const, label: 'Queue' },
  { key: 'history' as const, label: 'History' },
]
const TAB_KEYS = ['ArrowLeft', 'ArrowRight', 'Home', 'End']

function tabId(key: string): string {
  return `pr-tab-${key}`
}

function panelId(key: string): string {
  return `pr-panel-${key}`
}

function switchTab(key: 'queue' | 'history') {
  store.view = key
  if (key === 'history') void store.ensureHistoryLoaded(projectStore.activeWorkspace)
}

// Roving tabindex, mirroring ProjectView's project-tabs pattern: the bar is a
// single Tab stop and Left/Right/Home/End move (and switch) between the two.
function onReviewTabKeydown(event: KeyboardEvent): void {
  if (!TAB_KEYS.includes(event.key)) return
  const current = event.currentTarget as HTMLElement | null
  const bar = current?.parentElement
  if (!current || !bar) return
  const tabs = Array.from(bar.querySelectorAll<HTMLElement>('[role="tab"]'))
  const index = tabs.indexOf(current)
  if (index < 0) return
  event.preventDefault()
  let next = index
  if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
  else if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
  else if (event.key === 'Home') next = 0
  else next = tabs.length - 1
  const target = tabs[next]
  const key = target?.dataset.tab as 'queue' | 'history' | undefined
  if (!key) return
  switchTab(key)
  target.focus()
}

// Null until the ledger has loaded, and the badge is hidden while it is: a
// count rendered before the fetch read "History 0" on a ledger with hundreds
// of rows - the opposite of what a badge is for. `onMounted` prefetches, so
// the wait is the first request, not the first tab switch.
//
// Unfiltered it reports the server's scoped total rather than the rows we
// happen to hold: the page is capped, so a workspace with more decisions than
// the limit showed the limit itself (200) as though that were the whole
// ledger. Under a filter the visible count is the honest number.
const historyCount = computed(() => {
  if (!store.historyLoaded) return null
  if (store.historyFiltersActive) {
    return store.visibleHistory(projectStore.activeWorkspace).length
  }
  return store.historyTotal
})

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

/** The line under the title: where an accept would write.
 *
 * Every kind's answer lives in its descriptor, including the re-home row's
 * `from → to · why` form and the skill row's path.
 */
function rowSubtitle(row: ProposalRow): string {
  return descriptorFor(row).destination(row)
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

async function confirmAccept(row: ProposalRow) {
  // A region-kind row with a leak warning must be confirmed before the accept
  // is sent: accepting writes a region visible in every workspace.
  if (row.leak_warning) {
    confirmLeakId.value = row.id
    return
  }
  await acceptWithFallback(row)
}

async function doAccept(row: ProposalRow, workspace = '') {
  confirmLeakId.value = ''
  await acceptWithFallback(row, workspace)
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
    await store.fetch()
    // `store.fetch` deliberately leaves history alone, so this direct post -
    // the only mutation that does not go through `store.act` - has to say so
    // itself. Without it the accepted decision and the tab badge stayed stale
    // until the next mutation or workspace switch.
    store.invalidateHistory()
    return
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    const fallback = descriptorFor(row).fallback
    if (fallback && fallback.when(msg)) {
      await mergeViaChat(row, msg, fallback)
      return
    }
    store.error = msg
    return
  } finally {
    store.setBusy(row.id, false)
  }
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

function cancelLeakConfirm() {
  confirmLeakId.value = ''
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
    'nowhere. Do not edit anything: leave the proposal queued and I will accept or ' +
    'dismiss it myself.'
  )
}

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
    const lines = rows.map((r, i) => `${i + 1}. [${r.kind}] ${r.text}`).join('\n')
    projectStore.sendMessage(
      chat.chat_id,
      `${rows.length} queued proposals need a decision:\n\n${lines}\n\n` +
        'For each one, tell me whether it is durable and belongs where it says, ' +
        'or should be dropped. Do not edit any region or move any file: leave ' +
        'them queued and I will accept or dismiss them myself.',
    )
    pushBackgroundToast(chat.chat_id, 'Discussion in background', `${rows.length} proposals — click to open the chat`)
  } finally {
    chatBusy.value = false
  }
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
</script>

<template>
  <div class="proposal-review">
    <header class="pr-head">
      <!-- A tablist, not a nav landmark: this switches sub-views in place. -->
      <div class="pr-tabs" role="tablist" aria-label="Proposal review">
        <button
          v-for="tab in REVIEW_TABS"
          :key="tab.key"
          :id="tabId(tab.key)"
          type="button"
          class="pr-tab"
          :class="{ active: store.view === tab.key }"
          role="tab"
          :aria-selected="store.view === tab.key"
          :aria-controls="panelId(tab.key)"
          :tabindex="store.view === tab.key ? 0 : -1"
          :data-tab="tab.key"
          @click="switchTab(tab.key)"
          @keydown="onReviewTabKeydown"
        >
          {{ tab.label }}
          <span v-if="tab.key === 'history' && historyCount !== null" class="pr-tab-count">{{ historyCount }}</span>
        </button>
      </div>
      <p v-if="store.view === 'queue'" class="pr-summary">
        <strong>{{ filtered.length }}</strong> to review in {{ projectStore.activeWorkspace }}
        <button
          v-if="store.kindFilter !== 'all' || store.search"
          type="button"
          class="pr-clear-filter"
          @click="store.resetFilters()"
        >clear filter</button>
      </p>
    </header>

    <ProposalHistoryList
      v-if="store.view === 'history'"
      :id="panelId('history')"
      role="tabpanel"
      :aria-labelledby="tabId('history')"
    />

    <div v-else :id="panelId('queue')" role="tabpanel" :aria-labelledby="tabId('queue')">
    <p class="pr-hint">
      This is a fallback queue, not a list of every new fact: confident facts are
      applied when a chat is archived. Daily Memory curation retries addressable
      queued items and re-checks aging notes, so rows can disappear when either
      you or that run resolves them. Accepting a memory row writes it into that
      workspace’s bounded guide region; project rows fold into the named doc,
      people rows create a stub note, and learnings append to
      Workspace/Learnings.md. Re-home rows are not moved here. Skill rows are
      files — dismiss removes them, and implement builds the skill in a chat.
      Review rows have no destination yet and still need your decision.
    </p>


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
      >accept {{ selectedAcceptable.length }}</button>
      <!-- One destination for the whole selection. Re-home rows are moves, so
           "accept" cannot cover them: a move needs somewhere to go. -->
      <button
        v-for="target in batchMoveTargets"
        :key="`move-${target}`"
        type="button"
        class="btn-small btn-primary"
        :disabled="store.busy"
        @click="batchMove(target)"
      >move {{ selectedRehomeCount }} to {{ target }}</button>
      <button
        type="button"
        class="btn-small btn-chip"
        :disabled="store.busy"
        @click="batchDismiss"
      >dismiss {{ selectedVisible.length }}</button>
      <button
        type="button"
        class="btn-small btn-chip"
        :disabled="chatBusy"
        @click="batchDiscuss"
      >talk about {{ selectedVisible.length }}</button>
      <button type="button" class="btn-small btn-chip" @click="selected = new Set()">clear</button>
    </div>

    <p v-if="!filtered.length" class="pr-empty">Nothing queued here.</p>

    <section class="pr-group">
      <header v-if="filtered.length" class="pr-group-head">
        <label class="pr-group-select">
          <input type="checkbox" :checked="allSelected" @change="toggleAll" />
          <span class="pr-group-name">select all</span>
        </label>
        <span class="pr-group-count">{{ filtered.length }}</span>
      </header>

      <template v-for="group in groups" :key="group.key">
        <!-- Legacy dated proposals remain one decision until the next reflection
             upserts their canonical file. -->
        <header v-if="group.label" class="pr-group-label">
          <span class="pr-group-label-name">{{ group.label }}</span>
          <span class="pr-group-label-count">{{ group.rows.length }}</span>
        </header>
        <ul class="pr-rows">
        <li
          v-for="row in group.rows"
          :key="row.id"
          class="pr-row"
          :class="{ 'pr-row--leak': row.leak_warning, 'pr-row--busy': store.isBusy(row.id), 'pr-row--linked': hasActiveLink(row) }"
        >
          <input
            class="pr-row-check"
            type="checkbox"
            :checked="selected.has(row.id)"
            @change="toggleRow(row.id)"
          />

          <div class="pr-row-body">
            <div class="pr-row-top">
              <span class="pr-kind" :class="`pr-kind--${row.kind}`">{{ kindLabel(row.kind) }}</span>
              <span class="pr-row-title">{{ rowTitle(row) }}</span>
            </div>
            <!-- For a skill row the subtitle IS the file, so it opens it. A
                 separate "view" button spent a slot saying what the path already
                 said. Only the leaf: every row in a group shares the folder. -->
            <p class="pr-row-sub">
              <button
                v-if="isSkill(row) && row.path"
                type="button"
                class="pr-path-link"
                :title="row.path"
                @click="view(row)"
              >{{ pathLeaf(row.path) }}</button>
              <template v-else>{{ rowSubtitle(row) }}</template>
              <span v-if="row.leak_warning" class="pr-badge --warn">visible in every workspace</span>
            </p>
            <details v-if="rowDetail(row)" class="pr-row-detail">
              <summary>details</summary>
              <p class="pr-row-prose">{{ rowDetail(row) }}</p>
              <p class="pr-row-source">{{ row.path }}</p>
            </details>
          </div>

          <!-- Leak confirm replaces the actions until answered. -->
          <div v-if="confirmLeakId === row.id" class="pr-actions pr-actions--confirm">
            <span class="pr-confirm-text">Writes into a guide every workspace loads. Sure?</span>
            <button type="button" class="btn-small btn-primary" :disabled="store.isBusy(row.id)" @click="doAccept(row)">{{ store.isBusy(row.id) ? 'working…' : 'confirm' }}</button>
            <button type="button" class="btn-small btn-chip" @click="cancelLeakConfirm">cancel</button>
          </div>

          <!-- Linked: this proposal already spawned a merge/implement chat that
               is still active. The row stays queued while the agent works, so
               replace the accept/dismiss buttons with a link to that chat.
               If the chat was closed/archived without removing the row, the
               watcher clears the link and this collapses back to the normal
               actions. -->
          <div v-else-if="hasActiveLink(row)" class="pr-actions pr-actions--linked">
            <span class="pr-linked-label">Working in <strong>{{ linkedChatTitle(row) }}</strong></span>
            <button type="button" class="btn-small btn-primary" @click="openLinkedChat(row)">Open chat</button>
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
              class="btn-small btn-primary"
              :disabled="store.isBusy(row.id)"
              @click="doAccept(row, c)"
            >{{ store.isBusy(row.id) ? 'working…' : c }}</button>
            <button type="button" class="btn-small btn-chip" :disabled="store.isBusy(row.id)" @click="doDismiss(row)">dismiss</button>
            <button type="button" class="btn-small btn-chip" :disabled="chatBusy" @click="discuss(row)">talk about it</button>
          </div>

          <!-- A skill proposal is a FILE, so its actions are the ones a file
               has: read it, build it, or drop it. "Accept" for a region row means
               "write this fact"; for a proposed skill it means "implement it",
               which is a chat, not a write. -->
          <div v-else-if="isSkill(row)" class="pr-actions">
            <button
              type="button"
              class="btn-small btn-primary"
              :disabled="chatBusy"
              @click="implementSkill(row)"
            >implement</button>
            <button type="button" class="btn-small btn-chip" :disabled="store.isBusy(row.id)" @click="doDismiss(row)">{{ store.isBusy(row.id) ? 'working…' : 'dismiss' }}</button>
            <button type="button" class="btn-small btn-chip" :disabled="chatBusy" @click="discuss(row)">talk about it</button>
          </div>

          <div v-else class="pr-actions">
            <!-- No accept when nothing backs a destination: a button that cannot
                 do what it says is worse than absent. A justified re-home names
                 the destination on the button, because "accept" does not say that
                 a file is about to move. -->
            <button
              v-if="canAccept(row)"
              type="button"
              class="btn-small btn-primary"
              :disabled="store.isBusy(row.id)"
              @click="confirmAccept(row)"
            >{{ store.isBusy(row.id) ? 'working…' : (isRehome(row) ? `move to ${rehomeTarget(row)}` : 'accept') }}</button>
            <button type="button" class="btn-small btn-chip" :disabled="store.isBusy(row.id)" @click="doDismiss(row)">{{ store.isBusy(row.id) ? 'working…' : 'dismiss' }}</button>
            <button type="button" class="btn-small btn-chip" :disabled="chatBusy" @click="discuss(row)">talk about it</button>
          </div>
        </li>
        </ul>
      </template>
    </section>

    <footer v-if="filtered.length" class="pr-foot">
      <label class="pr-older">
        <span>dismiss anything older than</span>
        <input v-model.number="olderThanDays" type="number" min="1" max="365" class="pr-older-input" />
        <span>days</span>
      </label>
      <button type="button" class="btn-small btn-chip" :disabled="store.busy" @click="dismissOlder">dismiss old</button>
    </footer>
    </div>
  </div>
</template>

<style scoped>
/* One column, generous vertical rhythm, and every row the same shape. The old
   layout stacked three unrelated control rows above a list whose items were
   paragraphs, so nothing had a predictable position. */
.proposal-review {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.pr-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.pr-summary {
  margin: 0;
  font-size: 0.95rem;
}

.pr-counts {
  display: inline-flex;
  gap: var(--space-2);
  margin-left: var(--space-2);
  color: var(--fg2);
  font-size: 0.8rem;
}

.pr-hint {
  margin: 0;
  color: var(--fg2);
  font-size: 0.8rem;
  line-height: 1.5;
  max-width: none;
}

.pr-error {
  color: var(--error);
  font-size: 0.85rem;
}

.pr-empty {
  color: var(--fg2);
  font-size: 0.9rem;
  padding: var(--space-4) 0;
}

/* Queue / History tablist, matching ProjectView's project-tabs underline
   style so switching sub-views reads the same way across the app. */
.pr-tabs {
  display: flex;
  gap: var(--space-1);
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--space-2);
  overflow-x: auto;
}

.pr-tab {
  min-height: var(--touch);
  padding: var(--space-2) var(--space-3);
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font: 600 var(--text-sm) var(--font);
  white-space: nowrap;
}
.pr-tab:hover { color: var(--fg); background: var(--bg3); }
.pr-tab.active {
  border-bottom-color: var(--accent);
  color: var(--fg);
}
.pr-tab:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.pr-tab-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: calc(var(--text-xs) + var(--space-2));
  min-height: calc(var(--text-xs) + var(--space-1));
  margin-left: var(--space-1);
  padding: 0 var(--space-1);
  border-radius: var(--radius-pill);
  background: var(--bg3);
  color: var(--fg2);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
.pr-tab.active .pr-tab-count { color: var(--fg); }

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
.pr-actions {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-1);
  flex: none;
  min-width: 8.5rem;
}

.pr-actions--confirm {
  min-width: 12rem;
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
  margin-left: var(--space-2);
  background: none;
  border: none;
  padding: 0;
  color: var(--accent);
  font-size: 0.78rem;
  cursor: pointer;
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
  padding-bottom: var(--space-1);
  border-bottom: 1px solid var(--border);
}

.pr-group-select {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
}

.pr-group-name {
  font-weight: 600;
  font-size: 0.9rem;
  text-transform: lowercase;
}

.pr-group-count {
  margin-left: auto;
  color: var(--fg2);
  font-size: 0.8rem;
}

.pr-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.pr-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: start;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
}

.pr-row--leak {
  border-color: var(--warning);
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
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, var(--bg2));
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

.pr-row-check {
  margin-top: 0.2rem;
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

.pr-row-title {
  font-size: 0.95rem;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.pr-row-sub {
  margin: 0.25rem 0 0;
  color: var(--fg2);
  font-size: 0.8rem;
}

.pr-kind {
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
  margin-top: var(--space-2);
  font-size: 0.8rem;
  color: var(--fg2);
}

.pr-row-detail summary {
  cursor: pointer;
}

.pr-row-prose,
.pr-row-source {
  margin: var(--space-2) 0 0;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.pr-row-source {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.72rem;
  opacity: 0.75;
}

.pr-confirm-text {
  font-size: 0.8rem;
  color: var(--fg2);
}

.pr-foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
  color: var(--fg2);
  font-size: 0.8rem;
}

.pr-older {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-right: auto;
}

.pr-older-input {
  width: 4.5rem;
}

/* Stacked column keeps text full-width on both desktop and mobile. */
@media (max-width: 640px) {
  .pr-row {
    grid-template-columns: auto 1fr;
  }

  .pr-actions {
    grid-column: 1 / -1;
  }
}
</style>
