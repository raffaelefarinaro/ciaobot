<template>
  <section ref="root" class="msk rq-page" aria-labelledby="msk-title" :aria-busy="showSkeleton">
    <h2 id="msk-title" class="sr-only" tabindex="-1">Skills</h2>
    <p class="sr-only" role="status" aria-live="polite" aria-atomic="true" data-skills-live>{{ live }}</p>

    <ul v-if="showSkeleton" class="rq-skeleton" aria-hidden="true">
      <li v-for="n in 3" :key="n" class="rq-skeleton-row">
        <span class="rq-skeleton-bar rq-skeleton-bar--title"></span>
        <span class="rq-skeleton-bar rq-skeleton-bar--sub"></span>
      </li>
    </ul>
    <p v-if="showSkeleton" class="sr-only" role="status">Reading the suggestions…</p>

    <div v-else-if="skills.error && !list" class="rq-state rq-state--error" role="alert">
      <p class="rq-state-text">Could not read the suggestions. {{ skills.error }}</p>
      <button type="button" class="btn-small msk-retry" @click="skills.refresh()">Try again</button>
    </div>

    <template v-else>
      <div v-if="notice && noticeAt === 'top'" class="rq-notice" data-skills-notice>
        <span class="rq-notice-text">{{ notice.text }}</span>
        <button v-if="notice.undo" type="button" class="rq-link rq-undo" :disabled="acting || careRunning" @click="undo">Undo</button>
      </div>
      <p v-if="careRunning" class="rq-care-note">
        Workspace care is running and may change these files. Reject and Undo wait until it finishes.
      </p>

      <p v-if="!items.length" ref="emptyEl" class="rq-state rq-state-text" tabindex="-1" data-skills-empty>
        No skill suggestions are waiting. One appears here when Ciaobot writes a skill-edit proposal file in Workspace/Proposals.
      </p>

      <!-- Phone: one card at a time, swiped or decided with the buttons under it. -->
      <div v-else-if="deck" class="rq-deck">
        <template v-if="selected">
          <div class="rq-deck-top">
            <p class="rq-count" data-skills-count>{{ position }} of {{ items.length }}</p>
            <button
              type="button"
              class="rq-link rq-skip-link"
              :disabled="items.length < 2"
              :aria-label="`Skip: ${skillTitle(selected)}`"
              @click="skip"
            >Skip</button>
          </div>
          <ReviewDeck
            ref="deckRef"
            :item-key="selected.path"
            :title-id="detailTitleId"
            :behind="Math.min(2, items.length - 1)"
            :disabled="rejectLocked"
            dismiss-stamp="REJECT"
            @decide="op => decide(op, selected!)"
          >
            <ProposalDetail
              v-if="!selected.withheld"
              :proposal="skillProposal(selected)"
              :open-path="selected.open_path"
              :title-id="detailTitleId"
              :problems="selected.problems"
              compact
            />
            <div v-else class="msk-withheld">
              <h3 :id="detailTitleId" class="msk-withheld-title" tabindex="-1">{{ skillTitle(selected) }}</h3>
              <p class="msk-withheld-text">{{ WITHHELD }}</p>
            </div>
          </ReviewDeck>
          <p class="rq-swipe-hint" aria-hidden="true">
            <span>← swipe left to reject</span><span>swipe right to approve →</span>
          </p>
          <div class="rq-deck-actions">
            <button
              type="button"
              class="rq-btn"
              :disabled="rejectLocked"
              :aria-label="`Reject: ${skillTitle(selected)}`"
              @click="fling('dismiss')"
            ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>Reject</button>
            <button
              type="button"
              class="rq-btn rq-btn--primary"
              :disabled="approveLocked"
              :aria-label="`Approve in a chat: ${skillTitle(selected)}`"
              @click="fling('approve')"
            ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>Approve</button>
          </div>
          <p class="msk-how">{{ HOW }}</p>
          <div v-if="notice && noticeAt === 'deck'" class="rq-notice" data-skills-notice>
            <span class="rq-notice-text">{{ notice.text }}</span>
            <button v-if="notice.undo" type="button" class="rq-link rq-undo" :disabled="acting || careRunning" @click="undo">Undo</button>
          </div>
        </template>
      </div>

      <!-- Desktop: the suggestions on the left, the open one in full on the right. -->
      <div v-else class="rq-queue">
        <div class="rq-list-pane">
          <div class="rq-list-head">
            <h3 id="msk-list-title" class="rq-list-title">Suggestions</h3>
            <span class="rq-count" data-skills-count>{{ selected ? `${position} of ${items.length}` : items.length }}</span>
          </div>
          <ul
            ref="listEl"
            class="rq-list"
            role="listbox"
            aria-labelledby="msk-list-title"
            @keydown="onListKeydown"
          >
            <li
              v-for="(item, n) in items"
              :id="`msk-o-${n}`"
              :key="item.path"
              class="rq-row"
              :class="{ 'rq-row--current': item === selected }"
              role="option"
              :aria-selected="item === selected"
              :tabindex="item === selected ? 0 : -1"
              data-skills-row
              @click="select(item)"
            >
              <span class="rq-kind" aria-hidden="true">edit</span>
              <span class="rq-row-title">{{ skillTitle(item) }}</span>
              <span v-if="skillRowSub(item)" class="rq-row-sub">{{ skillRowSub(item) }}</span>
            </li>
          </ul>
        </div>

        <div v-if="selected" class="rq-detail" :aria-labelledby="detailTitleId" role="region">
          <div :key="selected.path" class="rq-detail-body">
            <ProposalDetail
              v-if="!selected.withheld"
              :proposal="skillProposal(selected)"
              :open-path="selected.open_path"
              :title-id="detailTitleId"
              :problems="selected.problems"
            />
            <div v-else class="msk-withheld">
              <h3 :id="detailTitleId" class="msk-withheld-title" tabindex="-1">{{ skillTitle(selected) }}</h3>
              <p class="msk-withheld-text">{{ WITHHELD }}</p>
            </div>
          </div>
          <div class="rq-detail-foot">
            <div v-if="notice && noticeAt === 'detail'" class="rq-notice" data-skills-notice>
              <span class="rq-notice-text">{{ notice.text }}</span>
              <button v-if="notice.undo" type="button" class="rq-link rq-undo" :disabled="acting || careRunning" @click="undo">Undo</button>
            </div>
            <div class="rq-detail-actions">
              <button
                type="button"
                class="rq-btn rq-btn--primary"
                :disabled="approveLocked"
                :aria-label="`Approve in a chat: ${skillTitle(selected)}`"
                aria-keyshortcuts="A"
                @click="approve(selected)"
              ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>Approve <kbd aria-hidden="true">A</kbd></button>
              <button
                type="button"
                class="rq-btn"
                :disabled="rejectLocked"
                :aria-label="`Reject: ${skillTitle(selected)}`"
                aria-keyshortcuts="D"
                title="Mark the proposal rejected; nothing else changes"
                @click="decide('dismiss', selected)"
              ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>Reject <kbd aria-hidden="true">D</kbd></button>
              <button
                type="button"
                class="rq-btn rq-btn--ghost"
                :disabled="items.length < 2"
                :aria-label="`Skip: ${skillTitle(selected)}`"
                aria-keyshortcuts="S"
                title="Leave it for later and show the next one"
                @click="skip"
              >Skip <kbd aria-hidden="true">S</kbd></button>
              <span class="rq-hint" aria-hidden="true">↑ ↓ move · acting moves to the next</span>
            </div>
            <p class="msk-how">{{ HOW }}</p>
          </div>
          <button
            type="button"
            class="rq-link msk-file"
            :aria-label="`Open the file: ${selected.path}`"
            @click="fileViewer.open(selected.open_path)"
          >Open the file</button>
        </div>
      </div>

      <p v-if="list && (list.skipped || list.truncated)" class="msk-skipped">{{ skippedWords }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ProposalDetail from './ProposalDetail.vue'
import ReviewDeck from './ReviewDeck.vue'
import { approveProposal, rejectProposal, unrejectProposal } from '../lib/proposalDecision'
import { keyBelongsElsewhere, queueKey } from '../lib/reviewQueue'
import { skillProposal, skillRowSub, skillTitle, type SkillProposalItem } from '../lib/skillProposals'
import { useCareReportStore } from '../stores/careReport'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import { useSkillProposalsStore } from '../stores/skillProposals'

/**
 * Memory · Skills: the open skill-edit proposals (`Workspace/Proposals/`, type
 * `skill-edit`, status `draft` or `ready`) as a review queue, in the same
 * shape as Memory · Unsure (shared styles in `styles/review-queue.css`, the
 * phone card `ReviewDeck`, the keys in `lib/reviewQueue`).
 *
 * Desktop: the suggestions on the left, the open one in full on the right
 * (`ProposalDetail`, the same view the care page's proposal review uses).
 * Phone (a pane under 640px): one card at a time, swiped right to approve and
 * left to reject. A approves, D rejects, S skips and the arrows move, unless a
 * text field, menu or dialog has the key.
 *
 * Approve is the care page's Approve: a new chat in General with the proposal
 * pinned and the request drafted, which the person sends; the skill changes
 * there, with them, and nothing is written from this page. Reject is the care
 * page's Reject: the engine's list and the file are read again, and only when
 * both still say the proposal is open is its `status:` line set to
 * `rejected` through the file viewer's save. Undo puts the previous status
 * back while the file still says `rejected`. Reject and Undo wait while
 * Workspace care runs.
 */
const store = useProjectStore()
const fileViewer = useFileViewerStore()
const skills = useSkillProposalsStore()
const care = useCareReportStore()

const HOW = 'Approve opens a chat with the request drafted; the skill is changed there, with you. Reject only marks the proposal rejected.'
const WITHHELD = 'This proposal holds text shaped like a credential, so the app does not show it. Open the file to read it, or reject it.'

const mountedOnWorkspace = skills.workspace === store.activeWorkspace
watch(() => store.activeWorkspace, ws => { skills.show(ws); care.show(ws) }, { immediate: true })

const list = computed(() => (skills.list && skills.list.workspace === store.activeWorkspace ? skills.list : null))
const items = computed(() => list.value?.items ?? [])
const showSkeleton = computed(() => !list.value && !skills.error)
const careRunning = computed(() => !!care.report?.running)
const skippedWords = computed(() => {
  const parts: string[] = []
  if (list.value?.skipped) parts.push(`${list.value.skipped} proposal ${list.value.skipped === 1 ? 'file was' : 'files were'} not read: a link, too large or unreadable.`)
  if (list.value?.truncated) parts.push('Only the first 200 proposal files were read.')
  return parts.join(' ')
})

const root = ref<HTMLElement | null>(null)
const listEl = ref<HTMLElement | null>(null)
const emptyEl = ref<HTMLElement | null>(null)
const deckRef = ref<InstanceType<typeof ReviewDeck> | null>(null)
const detailTitleId = 'msk-detail-title'

// ── Layout: a card deck when the pane is phone-narrow ─────────────────────────
const DECK_MAX_WIDTH = 640
const deck = ref(false)
let resizeObserver: ResizeObserver | null = null
function measure(width: number): void {
  if (width > 0) deck.value = width < DECK_MAX_WIDTH
}

// ── Selection: by path, then by place, so acting lands on the next one ────────
const selectedPath = ref('')
const selectedPlace = ref(0)
const selected = computed<SkillProposalItem | null>(() => {
  const all = items.value
  if (!all.length) return null
  return all.find(i => i.path === selectedPath.value) ?? all[Math.min(selectedPlace.value, all.length - 1)]
})
const position = computed(() => (selected.value ? items.value.indexOf(selected.value) + 1 : 0))
function select(item: SkillProposalItem): void {
  selectedPath.value = item.path
  selectedPlace.value = Math.max(0, items.value.indexOf(item))
}

const acting = ref(false)
const rejectLocked = computed(() => acting.value || careRunning.value)
const approveLocked = computed(() => acting.value || !!selected.value?.withheld)

// ── Saying what happened ────────────────────────────────────────────────────
const notice = ref<{ text: string; undo: boolean } | null>(null)
const noticeAt = computed(() => (!selected.value ? 'top' : deck.value ? 'deck' : 'detail'))
const live = ref('')
const lastReject = ref<{ path: string; openPath: string; previous: string; title: string } | null>(null)

function say(text: string): void {
  live.value = ''
  void nextTick(() => { live.value = text })
}
function where(): string {
  const item = selected.value
  return item ? `Now ${position.value} of ${items.value.length}: “${skillTitle(item)}”.` : 'Nothing else is waiting.'
}

async function settleFocus(): Promise<void> {
  await nextTick()
  const el = root.value
  if (!el) return
  const active = document.activeElement as HTMLElement | null
  const lost = !active || active === document.body || !active.isConnected
  const disabledHere = !!active && el.contains(active) && (active as HTMLButtonElement).disabled
  if (!lost && !disabledHere) return
  focusCurrent()
}
function focusCurrent(): void {
  const el = root.value
  if (!el) return
  const target = deck.value
    ? el.querySelector<HTMLElement>(`#${detailTitleId}`)
    : el.querySelector<HTMLElement>('[data-skills-row][aria-selected="true"]')
  ;(target ?? emptyEl.value ?? el.querySelector<HTMLElement>('#msk-title'))?.focus()
}

// ── Acting ──────────────────────────────────────────────────────────────────
async function approve(item: SkillProposalItem): Promise<void> {
  deckRef.value?.reset()
  if (item.withheld || acting.value) return
  acting.value = true
  try {
    const chat = await approveProposal(store, {
      openPath: item.open_path,
      proposal: skillProposal(item),
      title: skillTitle(item),
      workspace: list.value?.workspace || store.activeWorkspace,
    })
    if (chat) say('A new chat is open with the request. Read it, then press Send.')
  } finally {
    acting.value = false
  }
}

async function reject(item: SkillProposalItem): Promise<void> {
  if (acting.value) return
  if (careRunning.value) {
    deckRef.value?.reset()
    say('Workspace care is running; try again when it finishes.')
    return
  }
  const place = Math.max(0, items.value.indexOf(item))
  acting.value = true
  let text = ''
  let undoable = false
  try {
    const outcome = await rejectProposal(item.open_path, async () => {
      await Promise.all([skills.refresh(), care.refresh()])
      return !care.report?.running && items.value.some(i => i.path === item.path)
    })
    if (outcome.kind === 'rejected') {
      // The engine's own read of the status, not this page's parse of the file.
      lastReject.value = { path: item.path, openPath: item.open_path, previous: item.status, title: skillTitle(item) }
      text = `Rejected “${skillTitle(item)}”.`
      undoable = true
    } else {
      text = 'This proposal was already decided or changed, so nothing was written.'
    }
  } catch (cause) {
    text = cause instanceof Error ? cause.message : String(cause)
  }
  await skills.refresh()
  acting.value = false
  deckRef.value?.reset()
  selectedPath.value = ''
  selectedPlace.value = place
  notice.value = { text, undo: undoable }
  say(`${text} ${where()}`)
  await settleFocus()
}

function decide(op: 'approve' | 'dismiss', item: SkillProposalItem): void {
  if (op === 'approve') void approve(item)
  else void reject(item)
}

/** The buttons and keys on the phone send the card off the way a swipe does. */
function fling(op: 'approve' | 'dismiss'): void {
  const item = selected.value
  if (!item) return
  if (op === 'dismiss' && rejectLocked.value) {
    if (careRunning.value) say('Workspace care is running; try again when it finishes.')
    return
  }
  if (op === 'approve' && approveLocked.value) return
  if (deckRef.value) void deckRef.value.fling(op)
  else decide(op, item)
}

function skip(): void {
  const all = items.value
  const item = selected.value
  if (all.length < 2 || !item) return
  select(all[(all.indexOf(item) + 1) % all.length])
  say(`Skipped. ${where()}`)
}

async function undo(): Promise<void> {
  const action = lastReject.value
  if (!action || acting.value || careRunning.value) return
  acting.value = true
  let problem = ''
  try {
    problem = await unrejectProposal(action.openPath, action.previous)
  } catch (cause) {
    problem = cause instanceof Error ? cause.message : String(cause)
  }
  await skills.refresh()
  acting.value = false
  if (problem) { notice.value = { text: problem, undo: false }; say(problem); return }
  lastReject.value = null
  selectedPath.value = action.path
  const text = `“${action.title}” is back on the list.`
  notice.value = { text, undo: false }
  say(`${text} ${where()}`)
  await nextTick()
  focusCurrent()
}

// ── Keys ────────────────────────────────────────────────────────────────────
function move(step: 1 | -1): void {
  const all = items.value
  if (!all.length) return
  const at = selected.value ? all.indexOf(selected.value) : -1
  const next = all[Math.max(0, Math.min(all.length - 1, at + step))]
  if (next && next !== selected.value) select(next)
}

function onListKeydown(event: KeyboardEvent): void {
  const key = queueKey(event)
  if (key !== 'next' && key !== 'previous') return
  event.preventDefault()
  move(key === 'next' ? 1 : -1)
  void nextTick(focusCurrent)
}

function onWindowKeydown(event: KeyboardEvent): void {
  const key = queueKey(event)
  if (!key || !root.value?.isConnected || !items.value.length) return
  if (keyBelongsElsewhere(event.target, root.value)) return
  if (fileViewer.isOpen || document.querySelector('[data-dismissable-layer], [aria-modal="true"]')) return
  const item = selected.value
  if (!item) return
  if (key === 'next' || key === 'previous') {
    if (!deck.value || !(event.target instanceof Node) || !root.value.contains(event.target)) return
    event.preventDefault()
    move(key === 'next' ? 1 : -1)
    say(where())
    return
  }
  event.preventDefault()
  if (key === 'skip') { skip(); return }
  if (key === 'approve') {
    if (item.withheld) { say(WITHHELD); return }
    fling('approve')
    return
  }
  fling('dismiss')
}

// ── Lifecycle ───────────────────────────────────────────────────────────────
let releaseCare: () => void = () => {}
onMounted(() => {
  releaseCare = care.attach()
  if (mountedOnWorkspace) void skills.refresh()
  window.addEventListener('keydown', onWindowKeydown)
  if (root.value) {
    measure(root.value.clientWidth)
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(entries => measure(entries[0]?.contentRect.width ?? 0))
      resizeObserver.observe(root.value)
    }
  }
})
onBeforeUnmount(() => {
  releaseCare()
  window.removeEventListener('keydown', onWindowKeydown)
  resizeObserver?.disconnect()
})
watch(() => store.activeWorkspace, () => {
  selectedPath.value = ''
  selectedPlace.value = 0
  notice.value = null
  lastReject.value = null
})
</script>

<style scoped>
#msk-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.msk-how { margin: 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.45; max-width: 68ch; }
.msk-file { align-self: flex-start; }
.msk-skipped { margin: 0; color: var(--fg3); font-size: var(--text-sm); }
.msk-withheld { display: flex; flex-direction: column; gap: var(--space-3); }
.msk-withheld-title {
  margin: 0;
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-lg);
  font-weight: 700;
  overflow-wrap: anywhere;
}
.msk-withheld-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: var(--radius-xs); }
.msk-withheld-text { margin: 0; color: var(--fg2); line-height: 1.5; max-width: 62ch; }
</style>
