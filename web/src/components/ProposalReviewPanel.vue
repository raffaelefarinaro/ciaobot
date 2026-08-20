<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useProposalsStore } from '../stores/proposals'
import { useProjectStore } from '../stores/projects'
import type { ProposalRow } from '../lib/types'

const store = useProposalsStore()
const projectStore = useProjectStore()
const chatBusy = ref(false)

const kindFilter = ref('all')
const selected = ref<Set<string>>(new Set())
const confirmLeakId = ref('')
const olderThanDays = ref(30)

const kinds = computed(() => ['all', ...new Set(store.rows.map(r => r.kind))])

/** Rows for the workspace the sidebar has selected.
 *
 * Scoped rather than grouped: the workspace switcher on the left is where every
 * other page keeps this choice, and grouping in the list meant the workspace —
 * which decides where an accept writes — lived in a heading you had to scroll
 * back to. A row with no workspace is install-wide and shows under whichever is
 * active, because it applies to all of them.
 */
const filtered = computed(() => store.rows.filter(r =>
  (kindFilter.value === 'all' || r.kind === kindFilter.value) &&
  // No active workspace yet (a single-workspace install, or the store still
  // loading) shows everything. Hiding every row until a switcher reports a
  // selection would read as an empty queue.
  (!projectStore.activeWorkspace || !r.workspace
    || r.workspace === projectStore.activeWorkspace),
))

const counts = computed(() => {
  const tally = new Map<string, number>()
  for (const row of filtered.value) tally.set(row.kind, (tally.get(row.kind) ?? 0) + 1)
  return [...tally.entries()].sort((a, b) => b[1] - a[1]).map(([kind, n]) => ({ kind, n }))
})


const KIND_LABELS: Record<string, string> = {
  memory: 'memory',
  profile: 'profile',
  user: 'profile',
  rehome: 're-home',
  skill: 'skill',
}

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind
}

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

function rowSubtitle(row: ProposalRow): string {
  if (isRehome(row)) {
    const sig = row.rehome
    const from = row.workspace
    if (sig?.candidates?.length && sig.candidates.length > 1) {
      return `${from} → ${sig.candidates.join(' or ')} · tags name more than one`
    }
    if (sig?.destination) {
      const to = sig.destination.split('/')[0]
      return sig.justified
        ? `${from} → ${to} · tags back this`
        : `${from} → ${to} · no tag backs it`
    }
    return `${from} · no destination, needs a decision`
  }
  if (isSkill(row)) return 'a skill proposal file'
  return `ciao:${row.region ?? row.kind}`
}

/** The verbose original, kept behind a disclosure rather than on the surface. */
function rowDetail(row: ProposalRow): string {
  if (isRehome(row)) return row.text
  return row.source ? `from ${row.source}` : ''
}

/** Whether an accept can do what it says.
 *
 * A skill row has no accept descriptor on the server, and a re-home row with no
 * backed destination has nowhere to go — the old UI still rendered "Move to a
 * destination?" beside a confirm button that could not name one.
 */
function canAccept(row: ProposalRow): boolean {
  if (isSkill(row)) return false
  if (isRehome(row)) return rehomeMode(row) === 'accept'
  return true
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

/** Selected rows an accept can actually be performed on.
 *
 * The same predicate as a row's own accept button, and it has to be: the batch
 * bar offered "accept 1" for a re-home row whose own actions correctly showed no
 * accept at all, and accepting one drops the bullet while moving nothing — so a
 * batch could silently discard proposals the UI had just said it could not act
 * on. A skill row is excluded for the older reason: it is a file, not a bullet,
 * and `accept_for('skill')` raises on the server.
 */
const selectedAcceptable = computed(() => [...selected.value].filter(id => {
  const row = store.rows.find(r => r.id === id)
  return !!row && canAccept(row)
}))

function isRegionKind(row: ProposalRow): boolean {
  return row.kind === 'memory' || row.kind === 'profile' || row.kind === 'user'
}

function isRehome(row: ProposalRow): boolean {
  return row.kind === 'rehome'
}

function isSkill(row: ProposalRow): boolean {
  return row.kind === 'skill'
}

// How a rehome row is presented. Never a pre-filled one-click accept for a
// destination no tag backs: a single clean signal is a plain accept, multiple
// candidates are a picker, and no signal is a question.
function rehomeMode(row: ProposalRow): 'accept' | 'picker' | 'question' {
  const sig = row.rehome
  if (!sig) return 'question'
  if (sig.candidates.length > 1) return 'picker'
  if (sig.justified) return 'accept'
  return 'question'
}

function confirmAccept(row: ProposalRow) {
  // A region-kind row with a leak warning must be confirmed before the accept
  // is sent: accepting writes a region visible in every workspace.
  if (row.leak_warning) {
    confirmLeakId.value = row.id
    return
  }
  void store.act(row.id, 'accept')
}

function doAccept(row: ProposalRow) {
  confirmLeakId.value = ''
  void store.act(row.id, 'accept')
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

async function discuss(row: ProposalRow) {
  // The row stays queued: this is "talk about it", not a decision. The chat is
  // created in the row's OWN workspace, because a proposal from work discussed
  // in a personal chat is read against the wrong vault, the wrong guide and the
  // wrong people.
  if (chatBusy.value) return
  chatBusy.value = true
  try {
    const chat = await openWorkspaceChat(row.workspace, 'Proposal review')
    if (!chat) return
    projectStore.sendMessage(chat.chat_id, discussPrompt(row))
    const { router } = await import('../router')
    await router.push(`/chat/${chat.chat_id}`)
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
  return (
    `A memory proposal is waiting for a decision (${where}), for the ` +
    `\`ciao:${row.region || row.kind}\` region: ${row.text}\n\n` +
    'Tell me whether this is durable and cross-session enough to belong in the ' +
    'always-loaded region, or whether it belongs in a note instead. Do not edit ' +
    'the region: leave the proposal queued and I will accept or dismiss it.'
  )
}

function batchAccept() {
  void store.batch(selectedAcceptable.value, 'accept')
}

function batchDismiss() {
  void store.batch([...selected.value], 'dismiss')
}

async function batchDiscuss() {
  // One chat for the whole selection, in the active workspace, and the rows stay
  // queued. Opening a chat per row would be unusable at the counts this queue
  // reaches, and the rows are usually related — which is why they were selected
  // together.
  const rows = [...selected.value]
    .map(id => store.rows.find(r => r.id === id))
    .filter((r): r is ProposalRow => !!r)
  if (!rows.length || chatBusy.value) return
  chatBusy.value = true
  try {
    const chat = await openWorkspaceChat(projectStore.activeWorkspace, 'Proposal review')
    if (!chat) return
    const lines = rows.map((r, i) => `${i + 1}. [${r.kind}] ${r.text}`).join('\n')
    projectStore.sendMessage(
      chat.chat_id,
      `${rows.length} queued proposals need a decision:\n\n${lines}\n\n` +
        'For each one, tell me whether it is durable and belongs where it says, ' +
        'or should be dropped. Do not edit any region or move any file: leave ' +
        'them queued and I will accept or dismiss them myself.',
    )
    const { router } = await import('../router')
    await router.push(`/chat/${chat.chat_id}`)
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

onMounted(() => { void store.fetch() })
</script>

<template>
  <div class="proposal-review">
    <header class="pr-head">
      <p class="pr-summary">
        <strong>{{ filtered.length }}</strong> to review in {{ projectStore.activeWorkspace }}
        <span v-if="counts.length" class="pr-counts">
          <span v-for="c in counts" :key="c.kind" class="pr-count">{{ c.n }} {{ kindLabel(c.kind) }}</span>
        </span>
      </p>
      <div class="pr-seg" role="group" aria-label="Filter by kind">
        <button
          v-for="k in kinds"
          :key="k"
          type="button"
          :class="{ active: kindFilter === k }"
          @click="kindFilter = k"
        >{{ k === 'all' ? 'all' : kindLabel(k) }}</button>
      </div>
    </header>

    <p class="pr-hint">
      Accepting a memory row writes it into that workspace’s bounded guide region.
      Re-home rows are not moved here. Skill rows are files, so they can only be
      dismissed or discussed.
    </p>

    <div v-if="store.error" class="pr-error">{{ store.error }}</div>

    <div v-if="selected.size" class="pr-batch">
      <span class="pr-batch-count">{{ selected.size }} selected</span>
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
      <button
        type="button"
        class="btn-small btn-chip"
        :disabled="store.busy"
        @click="batchDismiss"
      >dismiss {{ selected.size }}</button>
      <button
        type="button"
        class="btn-small btn-chip"
        :disabled="chatBusy"
        @click="batchDiscuss"
      >talk about {{ selected.size }}</button>
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

      <ul class="pr-rows">
        <li
          v-for="row in filtered"
          :key="row.id"
          class="pr-row"
          :class="{ 'pr-row--leak': row.leak_warning }"
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
            <p class="pr-row-sub">
              {{ rowSubtitle(row) }}
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
            <button type="button" class="btn-small btn-primary" :disabled="store.busy" @click="doAccept(row)">confirm</button>
            <button type="button" class="btn-small btn-chip" @click="cancelLeakConfirm">cancel</button>
          </div>

          <!-- A rehome row with several candidates: pick one, never pre-filled. -->
          <div v-else-if="isRehome(row) && rehomeMode(row) === 'picker'" class="pr-actions">
            <span class="pr-confirm-text">Which workspace?</span>
            <button
              v-for="c in row.rehome!.candidates"
              :key="c"
              type="button"
              class="btn-small btn-primary"
              :disabled="store.busy"
              @click="doAccept(row)"
            >{{ c }}</button>
            <button type="button" class="btn-small btn-chip" :disabled="store.busy" @click="doDismiss(row)">dismiss</button>
            <button type="button" class="btn-small btn-chip" :disabled="chatBusy" @click="discuss(row)">talk about it</button>
          </div>

          <div v-else class="pr-actions">
            <!-- No accept when nothing backs a destination, and none for a skill
                 file: a button that cannot do what it says is worse than absent. -->
            <button
              v-if="canAccept(row)"
              type="button"
              class="btn-small btn-primary"
              :disabled="store.busy"
              @click="confirmAccept(row)"
            >accept</button>
            <button type="button" class="btn-small btn-chip" :disabled="store.busy" @click="doDismiss(row)">dismiss</button>
            <button type="button" class="btn-small btn-chip" :disabled="chatBusy" @click="discuss(row)">talk about it</button>
          </div>
        </li>
      </ul>
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
  color: var(--text-muted);
  font-size: 0.8rem;
}

.pr-hint {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.8rem;
  line-height: 1.5;
  max-width: 68ch;
}

.pr-error {
  color: var(--danger, #f87171);
  font-size: 0.85rem;
}

.pr-empty {
  color: var(--text-muted);
  font-size: 0.9rem;
  padding: var(--space-4) 0;
}

/* Segmented kind filter, matching the memory view's Graph/List control. */
.pr-seg {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 999px;
  overflow: hidden;
}

.pr-seg button {
  border: 0;
  background: transparent;
  color: var(--text-muted);
  padding: 0.25rem 0.7rem;
  font-size: 0.8rem;
  cursor: pointer;
}

.pr-seg button.active {
  background: var(--surface-3, rgba(255, 255, 255, 0.08));
  color: var(--text);
}

/* The batch bar appears only with a selection, so it never occupies space while
   reading. */
.pr-batch {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-2, 8px);
  background: var(--surface-2, rgba(255, 255, 255, 0.04));
}

.pr-batch-count {
  font-size: 0.8rem;
  color: var(--text-muted);
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
  color: var(--text-muted);
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
  border-radius: var(--radius-2, 8px);
  background: var(--surface-2, rgba(255, 255, 255, 0.03));
}

.pr-row--leak {
  border-color: var(--warn, #d29922);
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
  color: var(--text-muted);
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
  background: var(--surface-3, rgba(255, 255, 255, 0.08));
  color: var(--text-muted);
}

.pr-badge {
  margin-left: var(--space-2);
  font-size: 0.7rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
}

.pr-badge.--warn {
  background: rgba(210, 153, 34, 0.18);
  color: var(--warn, #d29922);
}

/* The original bullet is a paragraph of prose with a CLI incantation in it.
   Useful, but not at the top of every row. */
.pr-row-detail {
  margin-top: var(--space-2);
  font-size: 0.8rem;
  color: var(--text-muted);
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

.pr-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  justify-content: flex-end;
}

.pr-actions--confirm {
  flex-basis: 100%;
}

.pr-confirm-text {
  font-size: 0.8rem;
  color: var(--text-muted);
}

.pr-foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
  color: var(--text-muted);
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

/* One column on a narrow window: a three-column grid puts the buttons in a
   sliver otherwise. */
@media (max-width: 640px) {
  .pr-row {
    grid-template-columns: auto 1fr;
  }

  .pr-actions {
    grid-column: 1 / -1;
    justify-content: flex-start;
  }
}
</style>
