<template>
  <section ref="root" class="mu" aria-labelledby="mu-title" :aria-busy="showSkeleton">
    <h2 id="mu-title" class="sr-only" tabindex="-1">Unsure</h2>

    <!-- First read: three quiet placeholder rows, so the list arrives in place. -->
    <ul v-if="showSkeleton" class="mu-skeleton" aria-hidden="true">
      <li v-for="n in 3" :key="n" class="mu-skeleton-row">
        <span class="mu-skeleton-bar mu-skeleton-bar--title"></span>
        <span class="mu-skeleton-bar mu-skeleton-bar--sub"></span>
      </li>
    </ul>
    <p v-if="showSkeleton" class="sr-only" role="status">Reading the list…</p>

    <div v-else-if="unsure.error && !list" class="mu-state mu-state--error" role="alert">
      <p class="mu-state-text">Could not read the list. {{ unsure.error }}</p>
      <button type="button" class="btn-small mu-retry" @click="unsure.refresh()">Try again</button>
    </div>

    <p v-else-if="!items.length" class="mu-state mu-state-text">
      Nothing is waiting. Care adds a line here only when it is not sure enough to write something down.
    </p>

    <template v-else>
      <p class="mu-notice" role="status">{{ notice }}</p>
      <p v-if="careRunning" class="mu-care-note">
        Workspace care is running and may change this list. Approve, Reject and Undo wait until it finishes.
      </p>

      <template v-for="group in groups" :key="group.key">
        <div v-if="group.items.length" class="mu-group">
          <h3 :id="`mu-group-${group.key}`" class="mu-group-title" tabindex="-1" :data-group="group.key">
            {{ group.label }} <span class="mu-group-count">{{ group.items.length }}</span>
          </h3>
          <ul class="mu-list" :aria-labelledby="`mu-group-${group.key}`" :data-group="group.key" @keydown="roam">
            <li
              v-for="entry in group.items"
              :key="entry.key"
              class="mu-row"
              :class="{ 'mu-row--approved': entry.item.checked, 'mu-row--busy': unsure.busy.has(entry.item.id) }"
              tabindex="0"
              data-unsure-row
              :aria-labelledby="entry.titleId"
            >
              <div class="mu-row-main">
                <div class="mu-row-heading">
                  <span
                    class="mu-kind"
                    :class="{ 'mu-kind--chat': !appliesUnattended(entry.item) }"
                    :title="kindTitle(entry.item)"
                  >{{ entry.item.malformed ? 'line' : kindLabel(entry.item.kind) }}</span>
                  <span :id="entry.titleId" class="mu-row-title">{{ entry.item.title }}</span>
                </div>
                <p v-if="entry.item.reason || entry.item.source || entry.item.date" class="mu-row-sub" :title="subTitle(entry.item)">
                  <span v-if="entry.item.reason" class="mu-row-reason">Unsure because {{ plain(entry.item.reason) }}</span>
                  <span v-if="entry.item.source || entry.item.date" class="mu-row-where">{{ [entry.item.source, entry.item.date].filter(Boolean).join(' · ') }}</span>
                </p>
                <p v-if="entry.item.checked && appliesUnattended(entry.item)" class="mu-row-state">
                  Approved: applied by the next care run.
                  <button
                    type="button"
                    class="mu-link"
                    :disabled="care.starting || careRunning"
                    :aria-label="`Apply now: run care to apply ${entry.item.title}`"
                    @click="applyNow"
                  >{{ careRunning ? 'Care is running' : care.starting ? 'Starting…' : 'Apply now' }}</button>
                </p>
                <p v-else-if="entry.item.checked" class="mu-row-state">
                  Approved. {{ needsChatReason(entry.item) }}
                </p>
                <p v-else-if="!appliesUnattended(entry.item)" class="mu-row-note">{{ needsChatReason(entry.item) }}</p>
                <button
                  type="button"
                  class="mu-disclosure"
                  :aria-expanded="open.has(entry.key)"
                  :aria-controls="`${entry.titleId}-raw`"
                  @click="toggle(entry.key)"
                >
                  <svg class="mu-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 6 15 12 9 18" /></svg>
                  {{ open.has(entry.key) ? 'Hide the full line' : 'Show the full line' }}
                </button>
                <p v-if="open.has(entry.key)" :id="`${entry.titleId}-raw`" class="mu-raw">{{ entry.item.raw }}</p>
              </div>

              <div class="mu-row-actions">
                <template v-if="entry.item.checked">
                  <button
                    v-if="!entry.item.malformed"
                    type="button"
                    class="mu-action"
                    :disabled="locked(entry.item)"
                    :aria-label="`Undo approval: ${entry.item.title}`"
                    @click="act('untick', entry.item)"
                  >Undo</button>
                  <button
                    v-if="!appliesUnattended(entry.item)"
                    type="button"
                    class="mu-action"
                    :aria-label="`Discuss: ${entry.item.title}`"
                    @click="discussItem(entry.item)"
                  >Discuss</button>
                </template>
                <template v-else>
                  <button
                    type="button"
                    class="mu-action mu-action--approve"
                    :disabled="appliesUnattended(entry.item) && locked(entry.item)"
                    :aria-label="appliesUnattended(entry.item) ? `Approve: ${entry.item.title}` : `Approve in a chat: ${entry.item.title}`"
                    :title="appliesUnattended(entry.item) ? 'The next care run applies it' : 'Opens a chat, because this one cannot be applied unattended'"
                    @click="approve(entry.item)"
                  >Approve</button>
                  <button
                    type="button"
                    class="mu-action"
                    :disabled="locked(entry.item)"
                    :aria-label="`Reject: ${entry.item.title}`"
                    title="Remove this line from the list"
                    @click="act('remove', entry.item)"
                  >Reject</button>
                  <button
                    type="button"
                    class="mu-action"
                    :aria-label="`Discuss: ${entry.item.title}`"
                    title="Open a new chat about this item"
                    @click="discussItem(entry.item)"
                  >Discuss</button>
                </template>
              </div>
            </li>
          </ul>
        </div>
      </template>
    </template>

    <div class="mu-actions">
      <button type="button" class="mu-link" :disabled="!list?.found" :title="list ? `Open ${list.path}` : 'Open Unsure.md'" @click="openList">Open Unsure.md</button>
      <span aria-hidden="true">·</span>
      <button type="button" class="mu-link" :disabled="!items.length" title="Start a chat about the whole list" @click="discussAll">Go through it with Ciaobot</button>
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
import { roamFocus } from '../lib/listRoaming'
import {
  appliesUnattended, discussSeed, kindLabel, needsChatReason,
  type UnsureItem, type UnsureOp,
} from '../lib/unsureList'

/**
 * Memory · Unsure: the lines of `Workspace/Unsure.md`, one row each. Approve
 * ticks the line (the next care run applies it), Reject removes it, Discuss
 * opens a new chat about it. Every write goes through `/api/memory/unsure`,
 * which names the line by a hash of its text, so a stale page changes nothing.
 */
const store = useProjectStore()
const fileViewer = useFileViewerStore()
const unsure = useUnsureStore()
const care = useCareReportStore()

const mountedOnWorkspace = unsure.workspace === store.activeWorkspace
watch(() => store.activeWorkspace, ws => { unsure.show(ws); care.show(ws) }, { immediate: true })
let releaseCare: () => void = () => {}
onMounted(() => {
  releaseCare = care.attach()
  // Opening the page always reads the list again: `show` keeps what it has
  // for the same workspace, and the file may have changed since.
  if (mountedOnWorkspace) void unsure.refresh()
})
onBeforeUnmount(() => releaseCare())

const list = computed(() => (unsure.list && unsure.list.workspace === store.activeWorkspace ? unsure.list : null))
const items = computed(() => list.value?.items ?? [])
const showSkeleton = computed(() => !list.value && !unsure.error)
const careRunning = computed(() => !!care.report?.running)
const root = ref<HTMLElement | null>(null)

/** A write is in flight for this line, or care is running and may be editing the file. */
function locked(item: UnsureItem): boolean {
  return unsure.busy.has(item.id) || careRunning.value
}

const groups = computed(() => {
  const rows = items.value.map((item, index) => ({ item, key: `${item.id}-${index}`, titleId: `mu-t-${item.id}-${index}` }))
  return [
    { key: 'waiting', label: 'Waiting for you', items: rows.filter(r => !r.item.checked) },
    { key: 'approved', label: 'Approved', items: rows.filter(r => r.item.checked) },
  ]
})

const notice = ref('')
const open = ref<Set<string>>(new Set())
function toggle(key: string): void {
  const next = new Set(open.value)
  if (!next.delete(key)) next.add(key)
  open.value = next
}

function kindTitle(item: UnsureItem): string {
  if (item.malformed) return 'Not in the list\'s format'
  return item.qualifier ? `${kindLabel(item.kind)}: ${item.qualifier}` : kindLabel(item.kind)
}
/** A Markdown link reads as its text in a one-line summary; the full line keeps it. */
function plain(text: string): string {
  return text.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
}
function subTitle(item: UnsureItem): string {
  return [item.reason && `Unsure because ${plain(item.reason)}`, item.source && `source: ${item.source}`, item.date].filter(Boolean).join('; ')
}

/** Where the acted-on row was: its group and its place in it. */
function placeOf(item: UnsureItem): { group: string; index: number } {
  const group = item.checked ? 'approved' : 'waiting'
  const rows = groups.value.find(g => g.key === group)?.items ?? []
  return { group, index: rows.findIndex(r => r.item.id === item.id) }
}

/**
 * The focused button goes away with its row's state, so focus moves on: to the
 * row now in the same place (the next one), else the one before, else the
 * group's heading, else the other group's, else the page's.
 */
async function restoreFocus(place: { group: string; index: number }): Promise<void> {
  await nextTick()
  const el = root.value
  if (!el) return
  const rows = Array.from(el.querySelectorAll<HTMLElement>(`.mu-list[data-group="${place.group}"] [data-unsure-row]`))
  const target = rows[Math.min(Math.max(place.index, 0), rows.length - 1)]
    ?? el.querySelector<HTMLElement>(`.mu-group-title[data-group="${place.group}"]`)
    ?? el.querySelector<HTMLElement>('.mu-group-title')
    ?? el.querySelector<HTMLElement>('#mu-title')
  target?.focus()
}

async function act(op: UnsureOp, item: UnsureItem): Promise<void> {
  notice.value = ''
  const place = placeOf(item)
  const problem = await unsure.act(op, item)
  await restoreFocus(place)
  if (problem) { notice.value = problem; return }
  notice.value = op === 'tick'
    ? `Approved “${item.title}”. The next care run applies it.`
    : op === 'untick' ? `Approval undone for “${item.title}”.` : `Removed “${item.title}” from the list.`
}

function approve(item: UnsureItem): void {
  if (appliesUnattended(item)) void act('tick', item)
  else void discussItem(item)
}

async function discussItem(item: UnsureItem): Promise<void> {
  const path = list.value?.path
  if (!path) return
  const short = item.title.length > 60 ? `${item.title.slice(0, 57)}…` : item.title
  await startFileDiscussion(store, { path, seed: discussSeed(item, path), title: `Settle: ${short}` })
}

async function applyNow(): Promise<void> {
  const { message } = await care.runAgain()
  if (message) notice.value = message
}

function roam(event: KeyboardEvent): void {
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
  const row = (event.target as HTMLElement | null)
  if (!row?.matches('[data-unsure-row]')) return
  const rows = Array.from(document.querySelectorAll<HTMLElement>('.mu [data-unsure-row]'))
  if (roamFocus([rows], event.key, 'list', () => ({ group: 0, item: rows.indexOf(row) }), row)) event.preventDefault()
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
</script>

<style scoped>
.mu {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: 4px 0 16px;
  container-type: inline-size;
}

.mu-state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  margin: 0;
  padding: 18px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.mu-state-text { margin: 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.5; max-width: 62ch; }
.mu-state--error .mu-state-text { color: var(--error-text); }

/* Always in the page so the status is announced; takes no room while empty. */
.mu-notice:empty {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}
.mu-notice {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
}

.mu-care-note { margin: 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.45; }
.mu-group { display: flex; flex-direction: column; }
.mu-group-title:focus-visible, #mu-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.mu-group + .mu-group { margin-top: var(--space-4); }
.mu-group-title {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin: 0 0 6px;
  color: var(--fg3);
  font-size: var(--text-sm);
  font-weight: 600;
}
.mu-group-count { font-family: var(--font-mono); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }

.mu-list { margin: 0; padding: 0; list-style: none; border-top: 1px solid var(--border); }

/* A ListRow-style row: flat, hairline under it, title over one muted line,
   the actions in the end column. Not a link: the row holds its own buttons. */
.mu-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: var(--space-4);
  padding: 12px 2px;
  border-bottom: 1px solid var(--border);
  outline: none;
  transition: background 120ms var(--ease);
}
.mu-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
.mu-row--busy { opacity: 0.6; }

.mu-row-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.mu-row-heading { display: flex; align-items: flex-start; gap: 8px; min-width: 0; }

/* Squared tag (DESIGN: --radius-xs is for tags that must not read as counts).
   An item a chat settles has a dashed outline: shape and words, not colour. */
.mu-kind {
  flex: 0 0 auto;
  margin-top: 2px;
  padding: 1px 6px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.4;
}
.mu-kind--chat { border-style: dashed; }

.mu-row-title {
  display: -webkit-box;
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
  line-height: 1.35;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
}
.mu-row--approved .mu-row-title { color: var(--fg2); font-weight: 500; }

.mu-row-sub {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.4;
  white-space: nowrap;
}
.mu-row-reason { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.mu-row-where {
  flex: 0 0 auto;
  max-width: 55%;
  min-width: 0;
  overflow: hidden;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
}

.mu-row-state,
.mu-row-note {
  margin: 2px 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
}
.mu-row-note { color: var(--fg3); }

.mu-disclosure {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  align-self: flex-start;
  min-height: 28px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--fg3);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-xs);
}
.mu-disclosure:hover { color: var(--fg); }
.mu-disclosure:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.mu-chevron { transition: transform 160ms var(--ease); }
.mu-disclosure[aria-expanded='true'] .mu-chevron { transform: rotate(90deg); }

.mu-raw {
  margin: 2px 0 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.mu-row-actions { display: flex; align-items: center; gap: 4px; }
.mu-action {
  min-height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
  transition: background 120ms var(--ease), color 120ms var(--ease), border-color 120ms var(--ease);
}
.mu-action:hover:not(:disabled) { background: var(--bg3); color: var(--fg); }
.mu-action--approve { border-color: var(--border-strong); color: var(--fg); font-weight: 600; }
.mu-action:disabled { cursor: default; opacity: 0.55; }
.mu-action:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.mu-actions { display: flex; align-items: center; gap: 8px; color: var(--fg3); }
.mu-link {
  min-height: var(--touch);
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
}
.mu-row-state .mu-link { min-height: 28px; margin-left: 4px; }
.mu-link:hover:not(:disabled) { text-decoration: underline; text-underline-offset: 3px; }
.mu-link:disabled { color: var(--fg3); cursor: default; }
.mu-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }

@media (pointer: coarse) {
  .mu-link, .mu-action, .mu-disclosure, .mu-row-state .mu-link { min-height: var(--touch); min-width: var(--touch); }
}

/* Narrow list: the actions drop under the text, like Needs you. */
@container (max-width: 560px) {
  .mu-row { grid-template-columns: minmax(0, 1fr); gap: var(--space-2); }
  .mu-row-sub { flex-wrap: wrap; white-space: normal; }
  .mu-row-reason { white-space: normal; }
  .mu-row-where { max-width: none; white-space: normal; overflow-wrap: anywhere; }
  .mu-row-actions { flex-wrap: wrap; }
}

.mu-skeleton { margin: 0; padding: 0; list-style: none; border-top: 1px solid var(--border); }
.mu-skeleton-row { display: flex; flex-direction: column; gap: 8px; padding: 14px 2px; border-bottom: 1px solid var(--border); }
.mu-skeleton-bar { display: block; height: 10px; border-radius: 5px; background: var(--bg3); animation: mu-pulse 1.4s var(--ease) infinite alternate; }
.mu-skeleton-bar--title { width: 58%; height: 12px; }
.mu-skeleton-bar--sub { width: 36%; }
@keyframes mu-pulse { from { opacity: 0.55; } to { opacity: 1; } }

@media (prefers-reduced-motion: reduce) {
  .mu-skeleton-bar { animation: none; }
  .mu-chevron, .mu-row, .mu-action { transition: none; }
}
</style>
