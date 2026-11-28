<template>
  <section ref="root" class="mu rq-page" :class="{ 'mu--deck': deck }" aria-labelledby="mu-title" :aria-busy="showSkeleton">
    <h2 id="mu-title" class="sr-only" tabindex="-1">Unsure</h2>
    <!-- Every change is said here once: what was done and what is showing now. -->
    <p class="sr-only" role="status" aria-live="polite" aria-atomic="true" data-unsure-live>{{ live }}</p>

    <!-- First read: placeholder rows, so the queue arrives in place. -->
    <ul v-if="showSkeleton" class="rq-skeleton" aria-hidden="true">
      <li v-for="n in 3" :key="n" class="rq-skeleton-row">
        <span class="rq-skeleton-bar rq-skeleton-bar--title"></span>
        <span class="rq-skeleton-bar rq-skeleton-bar--sub"></span>
      </li>
    </ul>
    <p v-if="showSkeleton" class="sr-only" role="status">Reading the list…</p>

    <div v-else-if="unsure.error && !list" class="rq-state rq-state--error" role="alert">
      <p class="rq-state-text">Could not read the list. {{ unsure.error }}</p>
      <button type="button" class="btn-small mu-retry" @click="unsure.refresh()">Try again</button>
    </div>

    <template v-else>
      <!-- What the last action did, with its Undo, sits where the eye already is:
           over the open item's buttons, under the card, or here once nothing is left. -->
      <div v-if="notice && noticeAt === 'top'" class="rq-notice " data-unsure-notice>
        <span class="rq-notice-text">{{ notice.text }}</span>
        <button
          v-if="notice.undo"
          type="button"
          class="rq-link rq-undo"
          :disabled="undoing || careRunning"
          @click="undo"
        >Undo</button>
      </div>
      <p v-if="careRunning" class="rq-care-note">
        Workspace care is running and may change this list. Approve, Dismiss and Undo wait until it finishes.
      </p>

      <p v-if="!items.length" ref="emptyEl" class="rq-state rq-state-text" tabindex="-1">
        Nothing is waiting. Care adds a line here only when it is not sure enough to write something down.
      </p>

      <!-- Phone: one card at a time, swiped or decided with the buttons under it. -->
      <div v-else-if="deck" class="rq-deck">
        <template v-if="selected && !selected.checked">
          <div class="rq-deck-top">
            <p class="rq-count" data-unsure-count>{{ position }} of {{ waiting.length }}</p>
            <button
              type="button"
              class="rq-link rq-skip-link"
              :disabled="waiting.length < 2"
              :aria-label="`Skip: ${selected.title}`"
              @click="skip"
            >Skip</button>
          </div>
          <ReviewDeck
            ref="deckRef"
            :item-key="selected.id"
            :title-id="detailTitleId"
            :behind="Math.min(2, waiting.length - 1)"
            :disabled="locked(selected)"
            @decide="op => decide(op, selected!)"
          >
            <MemoryUnsureDetail :item="selected" :list-path="listPath" :title-id="detailTitleId" compact />
          </ReviewDeck>
          <p class="rq-swipe-hint" aria-hidden="true">
            <span>← swipe left to dismiss</span><span>swipe right to approve →</span>
          </p>
          <div class="rq-deck-actions">
            <button
              type="button"
              class="rq-btn"
              :disabled="locked(selected)"
              :aria-label="`Dismiss: ${selected.title}`"
              @click="fling('dismiss')"
            ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>Dismiss</button>
            <button
              type="button"
              class="rq-btn rq-btn--primary"
              :disabled="approveLocked(selected)"
              :aria-label="approveLabel(selected)"
              @click="effectOf(selected).chat ? discussItem(selected) : fling('approve')"
            ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>{{ effectOf(selected).chat ? 'Approve in a chat' : 'Approve' }}</button>
          </div>
          <div v-if="notice && noticeAt === 'deck'" class="rq-notice " data-unsure-notice>
        <span class="rq-notice-text">{{ notice.text }}</span>
        <button
          v-if="notice.undo"
          type="button"
          class="rq-link rq-undo"
          :disabled="undoing || careRunning"
          @click="undo"
        >Undo</button>
      </div>
        </template>
        <p v-else class="rq-state rq-state-text" tabindex="-1" data-unsure-done>
          Nothing else is waiting.
        </p>
        <div v-if="approved.length" class="mu-approved-note">
          <p class="mu-approved-text">{{ approved.length }} approved, saved by the next Workspace care run.</p>
          <button
            type="button"
            class="rq-link"
            :disabled="care.starting || careRunning"
            @click="applyNow"
          >{{ careRunning ? 'Care is running' : care.starting ? 'Starting…' : 'Apply now' }}</button>
        </div>
      </div>

      <!-- Desktop: the queue on the left, the open item in full on the right. -->
      <div v-else class="rq-queue">
        <div class="rq-list-pane">
          <div class="rq-list-head">
            <h3 id="mu-list-title" class="rq-list-title">Waiting for you</h3>
            <span class="rq-count" data-unsure-count>{{ selected && !selected.checked ? `${position} of ${waiting.length}` : waiting.length }}</span>
          </div>
          <ul
            ref="listEl"
            class="rq-list"
            role="listbox"
            aria-labelledby="mu-list-title"
            @keydown="onListKeydown"
          >
            <li v-if="!waiting.length" class="rq-list-empty" role="presentation">Nothing else is waiting.</li>
            <li
              v-for="entry in waitingRows"
              :id="entry.optionId"
              :key="entry.key"
              class="rq-row"
              :class="{ 'rq-row--current': entry.item === selected, 'rq-row--busy': unsure.busy.has(entry.item.id) }"
              role="option"
              :aria-selected="entry.item === selected"
              :tabindex="entry.item === selected ? 0 : -1"
              data-unsure-row
              @click="select(entry.item)"
            >
              <span class="rq-kind" :class="{ 'mu-kind--chat': effectOf(entry.item).chat }" aria-hidden="true">{{ entry.item.malformed ? 'line' : kindLabel(entry.item.kind) }}</span>
              <span class="rq-row-title">{{ entry.item.title }}</span>
              <span v-if="entry.sub" class="rq-row-sub">{{ entry.sub }}</span>
            </li>
            <li v-if="approvedRows.length" class="mu-group" role="group" aria-labelledby="mu-approved-title">
              <div id="mu-approved-title" class="mu-group-title" role="presentation">
                Approved, not saved yet <span class="mu-group-count">{{ approvedRows.length }}</span>
              </div>
              <ul class="mu-sublist" role="presentation">
                <li
                  v-for="entry in approvedRows"
                  :id="entry.optionId"
                  :key="entry.key"
                  class="rq-row mu-row--approved"
                  :class="{ 'rq-row--current': entry.item === selected }"
                  role="option"
                  :aria-selected="entry.item === selected"
                  :tabindex="entry.item === selected ? 0 : -1"
                  data-unsure-row
                  @click="select(entry.item)"
                >
                  <span class="rq-kind" :class="{ 'mu-kind--chat': effectOf(entry.item).chat }" aria-hidden="true">{{ entry.item.malformed ? 'line' : kindLabel(entry.item.kind) }}</span>
                  <span class="rq-row-title">{{ entry.item.title }}</span>
                </li>
              </ul>
            </li>
          </ul>
        </div>

        <div v-if="selected" class="rq-detail" :aria-labelledby="detailTitleId" role="region">
          <div :key="selected.id" class="rq-detail-body">
            <MemoryUnsureDetail :item="selected" :list-path="listPath" :title-id="detailTitleId" />
          </div>
          <div class="rq-detail-foot">
          <div v-if="notice && noticeAt === 'detail'" class="rq-notice " data-unsure-notice>
        <span class="rq-notice-text">{{ notice.text }}</span>
        <button
          v-if="notice.undo"
          type="button"
          class="rq-link rq-undo"
          :disabled="undoing || careRunning"
          @click="undo"
        >Undo</button>
      </div>
          <div class="rq-detail-actions">
            <template v-if="selected.checked">
              <p class="mu-approved-text">
                {{ effectOf(selected).chat ? 'Approved. It still needs a chat to apply it.' : 'Approved: saved by the next Workspace care run.' }}
              </p>
              <button
                v-if="!effectOf(selected).chat"
                type="button"
                class="rq-btn"
                :disabled="care.starting || careRunning"
                :aria-label="`Apply now: run care to save ${selected.title}`"
                @click="applyNow"
              >{{ careRunning ? 'Care is running' : care.starting ? 'Starting…' : 'Apply now' }}</button>
              <button
                v-if="!selected.malformed"
                type="button"
                class="rq-btn"
                :disabled="locked(selected)"
                :aria-label="`Undo approval: ${selected.title}`"
                @click="unapprove(selected)"
              >Undo approval</button>
            </template>
            <template v-else>
              <button
                type="button"
                class="rq-btn rq-btn--primary"
                :disabled="approveLocked(selected)"
                :aria-label="approveLabel(selected)"
                aria-keyshortcuts="A"
                @click="approve(selected)"
              ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>{{ effectOf(selected).chat ? 'Approve in a chat' : 'Approve' }} <kbd aria-hidden="true">A</kbd></button>
              <button
                type="button"
                class="rq-btn"
                :disabled="locked(selected)"
                :aria-label="`Dismiss: ${selected.title}`"
                aria-keyshortcuts="D"
                title="Remove this line from the list"
                @click="decide('dismiss', selected)"
              ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>Dismiss <kbd aria-hidden="true">D</kbd></button>
              <button
                type="button"
                class="rq-btn rq-btn--ghost"
                :disabled="waiting.length < 2"
                :aria-label="`Skip: ${selected.title}`"
                aria-keyshortcuts="S"
                title="Leave it for later and show the next one"
                @click="skip"
              >Skip <kbd aria-hidden="true">S</kbd></button>
            </template>
            <span class="rq-hint" aria-hidden="true">↑ ↓ move · acting moves to the next</span>
          </div>
          </div>
          <button
            type="button"
            class="rq-link mu-discuss"
            :aria-label="`Discuss: ${selected.title}`"
            title="Open a new chat about this item"
            @click="discussItem(selected)"
          >Discuss it with Ciaobot</button>
        </div>
      </div>
    </template>

    <div class="rq-actions">
      <button type="button" class="rq-link" :disabled="!list?.found" :title="list ? `Open ${list.path}` : 'Open Unsure.md'" @click="openList">Open Unsure.md</button>
      <span aria-hidden="true">·</span>
      <button type="button" class="rq-link" :disabled="!items.length" title="Start a chat about the whole list" @click="discussAll">Go through it with Ciaobot</button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import { useUnsureStore } from '../stores/unsure'
import { useCareReportStore } from '../stores/careReport'
import { startFileDiscussion } from '../lib/fileDiscussion'
import MemoryUnsureDetail from './MemoryUnsureDetail.vue'
import ReviewDeck from './ReviewDeck.vue'
import {
  appliesUnattended, approveEffect, discussSeed, kindLabel, unsureSources,
  type UnsureItem,
} from '../lib/unsureList'
import { keyBelongsElsewhere, queueKey } from '../lib/reviewQueue'

/**
 * Memory · Unsure as a review queue over the lines of `Workspace/Unsure.md`.
 *
 * Desktop: the waiting items on the left (two-line titles over the reason),
 * the open one in full on the right with what approving does. Phone (a pane
 * under 640px): one card at a time, swiped right to approve and left to
 * dismiss, with the same buttons under it. Approve ticks the line (the next
 * care run saves it; a category, repository or rule item opens a chat
 * instead), Dismiss removes it, Skip shows the next without writing. Acting
 * moves to the next item. A approves, D dismisses, S skips and the arrows
 * move, unless a text field, menu or dialog has the key. The last action can
 * be undone: an approval is unticked, a dismissal puts the exact line back.
 * Every write goes through `/api/memory/unsure`, which names the line by a
 * hash of its text, so a stale page changes nothing.
 */
const store = useProjectStore()
const fileViewer = useFileViewerStore()
const unsure = useUnsureStore()
const care = useCareReportStore()

const mountedOnWorkspace = unsure.workspace === store.activeWorkspace
watch(() => store.activeWorkspace, ws => { unsure.show(ws); care.show(ws) }, { immediate: true })

const list = computed(() => (unsure.list && unsure.list.workspace === store.activeWorkspace ? unsure.list : null))
const listPath = computed(() => list.value?.path ?? '')
const items = computed(() => list.value?.items ?? [])
const waiting = computed(() => items.value.filter(i => !i.checked))
const approved = computed(() => items.value.filter(i => i.checked))
/** The order the arrows move in: the waiting items, then the approved ones. */
const ordered = computed(() => [...waiting.value, ...approved.value])
const showSkeleton = computed(() => !list.value && !unsure.error)
const careRunning = computed(() => !!care.report?.running)

const root = ref<HTMLElement | null>(null)
const listEl = ref<HTMLElement | null>(null)
const emptyEl = ref<HTMLElement | null>(null)
const deckRef = ref<InstanceType<typeof ReviewDeck> | null>(null)
const detailTitleId = 'mu-detail-title'

// ── Layout: a card deck when the pane is phone-narrow ─────────────────────────
const DECK_MAX_WIDTH = 640
const deck = ref(false)
let resizeObserver: ResizeObserver | null = null
function measure(width: number): void {
  if (width > 0) deck.value = width < DECK_MAX_WIDTH
}

// ── Selection ────────────────────────────────────────────────────────────────
// Kept by id (two identical lines have different ones), then by line text (a
// put-back line's id is not known until the list answers), then by place, so the
// selection survives a re-read and acting lands on the item that took its place.
const selectedId = ref('')
const selectedRaw = ref('')
const selectedPlace = ref(0)
const selected = computed<UnsureItem | null>(() => {
  const all = ordered.value
  if (!all.length) return null
  const exact = all.find(i => i.id === selectedId.value) ?? all.find(i => i.raw === selectedRaw.value)
  if (exact) return exact
  if (waiting.value.length) return waiting.value[Math.min(selectedPlace.value, waiting.value.length - 1)]
  return all[0]
})
/** 1-based place of the open item among the waiting ones. */
const position = computed(() => (selected.value ? waiting.value.indexOf(selected.value) + 1 : 0))

/** Show `item`, or the line with this text once the list has it. */
function pick(raw: string): void {
  selectedId.value = ''
  selectedRaw.value = raw
}
function select(item: UnsureItem): void {
  selectedId.value = item.id
  selectedRaw.value = item.raw
  const at = waiting.value.indexOf(item)
  if (at >= 0) selectedPlace.value = at
}

function rowSub(item: UnsureItem): string {
  const reason = item.reason ? `Unsure because ${item.reason.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')}` : ''
  const chats = unsureSources(item).filter(s => s.kind === 'chat').map(s => (s as { id: string }).id)
  return [reason, chats.join(', ')].filter(Boolean).join(' · ')
}
const waitingRows = computed(() => waiting.value.map((item, n) => ({ item, key: `${item.id}-w${n}`, optionId: `mu-o-${item.id}`, sub: rowSub(item) })))
const approvedRows = computed(() => approved.value.map((item, n) => ({ item, key: `${item.id}-a${n}`, optionId: `mu-o-${item.id}` })))

function effectOf(item: UnsureItem) {
  return approveEffect(item)
}
/** A write is in flight for this line, or care is running and may be editing the file. */
function locked(item: UnsureItem): boolean {
  return unsure.busy.has(item.id) || careRunning.value || acting.value
}
/** Approve opens a chat for what care cannot apply, so only a tick waits on care. */
function approveLocked(item: UnsureItem): boolean {
  return approveEffect(item).blocked || (appliesUnattended(item) && locked(item))
}
function approveLabel(item: UnsureItem): string {
  return appliesUnattended(item) ? `Approve: ${item.title}` : `Approve in a chat: ${item.title}`
}

// ── Saying what happened ────────────────────────────────────────────────────
interface LastAction { op: 'approve' | 'dismiss'; raw: string; before: string; after: string; section: string; title: string }
const notice = ref<{ text: string; undo: boolean } | null>(null)
/** Where the notice shows: over the open item's buttons, under the card, or at the top once no item is open. */
const noticeAt = computed(() => (!selected.value ? 'top' : deck.value ? (selected.value.checked ? 'top' : 'deck') : 'detail'))
const live = ref('')
const lastAction = ref<LastAction | null>(null)
const acting = ref(false)
const undoing = ref(false)

function say(text: string): void {
  // The same sentence twice must still be read out: clear, then set.
  live.value = ''
  void nextTick(() => { live.value = text })
}
function where(): string {
  const item = selected.value
  if (!item) return 'Nothing else is waiting.'
  if (item.checked) return `Showing an approved item: “${item.title}”.`
  return `Now ${position.value} of ${waiting.value.length}: “${item.title}”.`
}

function ticked(raw: string): string {
  return raw.replace(/^(\s*[-*]\s*\[) \]/, '$1x]')
}

// ── Focus after acting ──────────────────────────────────────────────────────
/**
 * The acted-on row or card goes away, so focus would fall to the page. It
 * moves to the item that took its place: its row on the desktop, its title on
 * the phone, else the empty line. A button that is still there keeps focus.
 */
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
    ? el.querySelector<HTMLElement>(`#${detailTitleId}`) ?? el.querySelector<HTMLElement>('[data-unsure-done]')
    : el.querySelector<HTMLElement>('[data-unsure-row][aria-selected="true"]')
  ;(target ?? emptyEl.value ?? el.querySelector<HTMLElement>('#mu-title'))?.focus()
}

// ── Acting ──────────────────────────────────────────────────────────────────
async function decide(op: 'approve' | 'dismiss', item: UnsureItem): Promise<void> {
  // A destination the engine refuses is never approved, by a swipe, a key or a click.
  if (op === 'approve' && approveEffect(item).blocked) {
    deckRef.value?.reset()
    say('This line names a place Ciaobot will not write to, so it cannot be approved here. Dismiss it, or discuss it.')
    return
  }
  if (op === 'approve' && !appliesUnattended(item)) {
    deckRef.value?.reset()
    await discussItem(item)
    return
  }
  if (locked(item)) {
    deckRef.value?.reset()
    if (careRunning.value) say('Workspace care is running; try again when it finishes.')
    return
  }
  const place = Math.max(0, waiting.value.indexOf(item))
  const at = items.value.indexOf(item)
  // Its neighbours and heading, so an Undo puts it back where it was.
  const before = items.value[at + 1]?.id ?? ''
  const after = items.value[at - 1]?.id ?? ''
  acting.value = true
  const problem = await unsure.act(op === 'approve' ? 'tick' : 'remove', item)
  acting.value = false
  deckRef.value?.reset()
  if (problem) {
    notice.value = { text: problem, undo: false }
    say(problem)
    await settleFocus()
    return
  }
  lastAction.value = { op, raw: item.raw, before, after, section: item.section, title: item.title }
  pick('')
  selectedPlace.value = place
  const done = op === 'approve'
    ? `Approved “${item.title}”. The next Workspace care run saves it.`
    : `Dismissed “${item.title}”.`
  notice.value = { text: done, undo: true }
  say(`${done} ${where()}`)
  await settleFocus()
}

function approve(item: UnsureItem): void {
  if (appliesUnattended(item)) void decide('approve', item)
  else void discussItem(item)
}

/** The buttons and keys on the phone send the card off the way a swipe does. */
function fling(op: 'approve' | 'dismiss'): void {
  const item = selected.value
  if (!item) return
  if (locked(item)) {
    if (careRunning.value) say('Workspace care is running; try again when it finishes.')
    return
  }
  if (deckRef.value) void deckRef.value.fling(op)
  else void decide(op, item)
}

function skip(): void {
  const all = waiting.value
  const item = selected.value
  if (all.length < 2 || !item) return
  const at = all.indexOf(item)
  const next = all[(at + 1) % all.length]
  select(next)
  say(`Skipped. ${where()}`)
}

async function unapprove(item: UnsureItem): Promise<void> {
  const problem = await unsure.act('untick', item)
  if (problem) { notice.value = { text: problem, undo: false }; say(problem); return }
  pick(item.raw.replace(/^(\s*[-*]\s*\[)[xX]\]/, '$1 ]'))
  notice.value = { text: `Approval undone for “${item.title}”.`, undo: false }
  lastAction.value = null
  say(`Approval undone for “${item.title}”. ${where()}`)
  await settleFocus()
}

/** Take back the last Approve (untick it) or Dismiss (put the exact line back), and show that item again. */
async function undo(): Promise<void> {
  const action = lastAction.value
  if (!action || undoing.value) return
  undoing.value = true
  let problem = ''
  if (action.op === 'approve') {
    const now = items.value.find(i => i.raw === ticked(action.raw))
    problem = now ? await unsure.act('untick', now) : 'That line is no longer on the list, so there is nothing to undo.'
  } else {
    problem = await unsure.restore(action.raw, action.before, action.after, action.section)
  }
  undoing.value = false
  if (problem) { notice.value = { text: problem, undo: false }; say(problem); return }
  lastAction.value = null
  pick(action.raw)
  const text = action.op === 'approve' ? `Approval undone for “${action.title}”.` : `“${action.title}” is back on the list.`
  notice.value = { text, undo: false }
  say(`${text} ${where()}`)
  await nextTick()
  focusCurrent()
}

// ── Keys ────────────────────────────────────────────────────────────────────
function move(step: 1 | -1): boolean {
  // The deck shows only what waits; the desktop list also holds the approved.
  const all = deck.value ? waiting.value : ordered.value
  if (!all.length) return false
  const at = selected.value ? all.indexOf(selected.value) : -1
  const next = all[Math.max(0, Math.min(all.length - 1, at + step))]
  if (!next || next === selected.value) return true
  select(next)
  return true
}

/** Arrows on a focused row move the selection and focus with it; the row announces itself. */
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
  // A menu, sheet, confirm or the file viewer over the page owns the keyboard.
  if (fileViewer.isOpen || document.querySelector('[data-dismissable-layer], [aria-modal="true"]')) return
  const item = selected.value
  if (!item) return
  // The arrows belong to the list (its own handler) so the page and the detail
  // still scroll with them; only the phone's card, which has no list, takes them.
  if (key === 'next' || key === 'previous') {
    if (!deck.value || !(event.target instanceof Node) || !root.value.contains(event.target)) return
    event.preventDefault()
    move(key === 'next' ? 1 : -1)
    say(where())
    return
  }
  event.preventDefault()
  if (key === 'skip') { skip(); return }
  if (item.checked) { say('This one is already approved.'); return }
  if (key === 'approve') {
    if (approveEffect(item).blocked) { say('This line names a place Ciaobot will not write to, so it cannot be approved here. Dismiss it, or discuss it.'); return }
    if (!appliesUnattended(item)) { void discussItem(item); return }
    fling('approve')
    return
  }
  fling('dismiss')
}

// ── Chats and the file ──────────────────────────────────────────────────────
async function discussItem(item: UnsureItem): Promise<void> {
  const path = list.value?.path
  if (!path) return
  const short = item.title.length > 60 ? `${item.title.slice(0, 57)}…` : item.title
  await startFileDiscussion(store, { path, seed: discussSeed(item, path), title: `Settle: ${short}` })
}

async function applyNow(): Promise<void> {
  const { message } = await care.runAgain()
  if (message) { notice.value = { text: message, undo: false }; say(message) }
}

function openList(): void {
  if (list.value) void fileViewer.open(list.value.path)
}
async function discussAll(): Promise<void> {
  if (!list.value) return
  await startFileDiscussion(store, {
    path: list.value.path,
    seed: 'Let\'s go through the Unsure list. Tick what is right, delete what is wrong, and write down what I confirm.',
    title: 'Go through the Unsure list',
  })
}

// ── Lifecycle ───────────────────────────────────────────────────────────────
let releaseCare: () => void = () => {}
onMounted(() => {
  releaseCare = care.attach()
  // Opening the page always reads the list again: `show` keeps what it has
  // for the same workspace, and the file may have changed since.
  if (mountedOnWorkspace) void unsure.refresh()
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
// A new workspace is a new queue: start from its first item, with nothing to undo.
watch(() => store.activeWorkspace, () => {
  pick('')
  selectedPlace.value = 0
  notice.value = null
  lastAction.value = null
})
</script>

<style scoped>
#mu-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.mu-sublist { margin: 0; padding: 0; list-style: none; }
.mu-kind--chat { border-style: dashed; }
.mu-row--approved .rq-row-title { color: var(--fg3); font-weight: 500; }
.mu-group-title {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  padding: 14px 16px 6px;
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-size: var(--text-sm);
  font-weight: 600;
}
.mu-group-count { font-family: var(--font-mono); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
.mu-discuss { align-self: flex-start; }
.mu-approved-text { flex: 1 1 100%; margin: 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.45; }
.mu-approved-note { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }
.mu-approved-note .mu-approved-text { flex: 1 1 200px; }
</style>
