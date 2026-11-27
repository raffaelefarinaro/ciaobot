<template>
  <DialogRoot :open="open" @update:open="value => !value && close()">
    <DialogPortal>
      <DialogOverlay class="chat-search-backdrop" />
      <DialogContent class="chat-search" aria-describedby="chat-search-help" @open-auto-focus="focusSearch" @close-auto-focus="preventDialogRestore">
        <DialogTitle class="chat-search-title">Search chats</DialogTitle>
        <DialogDescription id="chat-search-help" class="chat-search-help">Search saved conversations in {{ workspace }}.</DialogDescription>
        <label class="sr-only" for="chat-search-input">Title or conversation text</label>
        <div class="chat-search-field">
          <svg class="chat-search-glyph" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></svg>
          <input
            id="chat-search-input" ref="input" v-model="query" type="search" autocomplete="off" class="chat-search-input"
            placeholder="Title or conversation text" role="combobox" aria-autocomplete="list" aria-haspopup="listbox"
            :aria-expanded="items.length > 0" aria-controls="chat-search-results"
            :aria-activedescendant="activeIndex >= 0 ? optionId(activeIndex) : undefined"
            @keydown.down.prevent="moveActive(1)" @keydown.up.prevent="moveActive(-1)" @keydown.enter.prevent="submit"
          />
        </div>
        <p v-if="status" class="chat-search-status" role="status" aria-live="polite">{{ status }}</p>
        <button v-if="retryAction" type="button" class="chat-search-more chat-search-retry" @click="retry">Try again</button>
        <ul v-if="items.length" id="chat-search-results" class="chat-search-results" role="listbox" aria-label="Matching chats">
          <li
            v-for="(item, index) in items" :id="optionId(index)" :key="item.chat_id" role="option" class="chat-search-result"
            :class="{ 'chat-search-result--active': index === activeIndex }" :aria-selected="index === activeIndex"
            @click="select(item.chat_id)"
          >
            <span class="chat-search-result-title"><template v-for="(part, i) in highlight(item.title)" :key="i"><mark v-if="part.match" class="chat-search-mark">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
            <span v-if="item.project_title || item.archived" class="chat-search-meta">
              <span v-if="item.project_title" class="chat-search-project">{{ item.project_title }}</span>
              <span v-if="item.archived" class="chat-search-chip">Archived</span>
            </span>
            <span v-if="item.snippet" class="chat-search-snippet"><template v-for="(part, i) in highlight(plainText(item.snippet))" :key="i"><mark v-if="part.match" class="chat-search-mark">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
          </li>
        </ul>
        <button v-if="nextCursor && !retryAction" class="chat-search-more" type="button" :disabled="loading" @click="searchMore">Search more</button>
        <p v-if="skippedLarge" class="chat-search-help">Some large transcripts were skipped. A title match may still appear.</p>
        <div class="chat-search-actions"><button type="button" class="chat-search-close" @click="close">Close</button></div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<script setup lang="ts">
import { DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../lib/api'
import { highlightSegments, plainText } from '../lib/searchText'

type SearchItem = { chat_id: string; title: string; project_title: string; archived: boolean; snippet: string }
const props = defineProps<{ open: boolean; workspace: string }>()
const emit = defineEmits<{ close: []; select: [chatId: string] }>()
const input = ref<HTMLInputElement | null>(null)
const query = ref('')
const items = ref<SearchItem[]>([])
const nextCursor = ref<string | null>(null)
const skippedLarge = ref(false)
const loading = ref(false)
const status = ref('')
const activeIndex = ref(-1)
const shownQuery = ref('')
let generation = 0
let timer: ReturnType<typeof setTimeout> | undefined
const retryAction = ref<{ cursor: string | null; append: boolean } | null>(null)
let invokingElement: HTMLElement | null = null
let restoreFocusOnClose = true

function reset() {
  generation += 1
  clearTimeout(timer)
  items.value = []
  nextCursor.value = null
  skippedLarge.value = false
  loading.value = false
  retryAction.value = null
  status.value = ''
  query.value = ''
  activeIndex.value = -1
  shownQuery.value = ''
}
function optionId(index: number) { return `chat-search-option-${index}` }
function highlight(text: string) { return highlightSegments(text, shownQuery.value) }
function moveActive(step: 1 | -1) {
  if (!items.value.length) return
  activeIndex.value = Math.max(-1, Math.min(items.value.length - 1, activeIndex.value + step))
  if (activeIndex.value >= 0) void nextTick(() => document.getElementById(optionId(activeIndex.value))?.scrollIntoView({ block: 'nearest' }))
}
function focusSearch(event: Event) {
  event.preventDefault()
  requestAnimationFrame(() => input.value?.focus())
}
function preventDialogRestore(event: Event) { event.preventDefault() }
function close() { reset(); emit('close') }
async function run(cursor: string | null = null, append = false) {
  const q = query.value.trim()
  const token = ++generation
  retryAction.value = null
  if (q.length < 2) { items.value = []; status.value = ''; loading.value = false; return }
  loading.value = true
  status.value = 'Searching…'
  try {
    const result = await api.post<{ items: SearchItem[]; next_cursor: string | null; skipped_large_transcripts: boolean }>('/api/chats/search', { workspace: props.workspace, q, ...(cursor ? { cursor } : {}) })
    if (token !== generation || !props.open) return
    items.value = append ? [...items.value, ...result.items] : result.items
    shownQuery.value = q
    if (!append) activeIndex.value = -1
    nextCursor.value = result.next_cursor
    skippedLarge.value = append
      ? skippedLarge.value || result.skipped_large_transcripts
      : result.skipped_large_transcripts
    retryAction.value = null
    status.value = items.value.length ? `${items.value.length} results` : 'No matching chats.'
  } catch {
    if (token === generation) {
      retryAction.value = { cursor, append }
      status.value = append ? 'Search more failed. Try again.' : 'Search failed. Try again.'
    }
  } finally {
    if (token === generation) loading.value = false
  }
}
function submit() {
  const chosen = items.value[activeIndex.value]
  if (chosen) { select(chosen.chat_id); return }
  clearTimeout(timer)
  void run()
}
function searchMore() { if (nextCursor.value) void run(nextCursor.value, true) }
function retry() {
  if (retryAction.value) void run(retryAction.value.cursor, retryAction.value.append)
}
function select(chatId: string) { restoreFocusOnClose = false; reset(); emit('select', chatId) }
watch(() => [props.open, props.workspace, query.value] as const, ([open], previous) => {
  generation += 1
  clearTimeout(timer)
  if (!open) return
  if (previous && (previous[1] !== props.workspace || previous[2] !== query.value)) {
    items.value = []
    nextCursor.value = null
    skippedLarge.value = false
    loading.value = false
    retryAction.value = null
    status.value = ''
    activeIndex.value = -1
  }
  if (query.value.trim().length >= 2) timer = setTimeout(() => void run(), 250)
  else { items.value = []; nextCursor.value = null; status.value = ''; activeIndex.value = -1 }
})
onBeforeUnmount(() => clearTimeout(timer))
watch(() => props.open, (open) => {
  if (open) {
    restoreFocusOnClose = true
    const active = document.activeElement
    invokingElement = active instanceof HTMLElement && active !== document.body ? active : null
    return
  }
  const target = invokingElement
  invokingElement = null
  if (restoreFocusOnClose && target) void nextTick(() => { if (target.isConnected) target.focus() })
}, { flush: 'sync', immediate: true })
</script>

<style scoped>
.chat-search-backdrop { position: fixed; inset: 0; z-index: 1000; background: rgb(0 0 0 / 55%); }
.chat-search { position: fixed; z-index: 1001; inset: 50% auto auto 50%; transform: translate(-50%, -50%); width: min(560px, calc(100vw - 32px)); max-height: min(80vh, 720px); overflow: auto; padding: var(--space-5); background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); color: var(--fg); }
.chat-search-title { margin: 0; font-size: 1.1rem; }
.chat-search-help { margin: var(--space-1) 0 0; color: var(--fg2); font-size: var(--text-base); }
.chat-search-field { position: relative; margin-top: var(--space-4); }
.chat-search-glyph { position: absolute; top: 50%; left: var(--space-3); transform: translateY(-50%); color: var(--fg3); pointer-events: none; }
.chat-search-input { box-sizing: border-box; width: 100%; min-height: 44px; padding: var(--space-2) var(--space-3) var(--space-2) calc(var(--space-3) + 18px + var(--space-2)); color: var(--fg); background: var(--bg2); border: 1px solid var(--border-strong); border-radius: var(--radius); font: inherit; }
.chat-search-input::placeholder { color: var(--fg3); }
.chat-search-status { margin: var(--space-4) 0 var(--space-2); color: var(--fg2); font-size: var(--text-base); }
.chat-search-results { list-style: none; margin: var(--space-2) 0 var(--space-3); padding: 0; border-top: 1px solid var(--border); }
.chat-search-result { display: grid; gap: var(--space-1); min-height: 64px; padding: var(--space-4) var(--space-3); border-bottom: 1px solid var(--border); text-align: left; cursor: pointer; }
.chat-search-result:hover, .chat-search-result--active { background: var(--bg2); }
.chat-search-result--active { box-shadow: inset 2px 0 0 var(--accent); }
.chat-search-result-title { font-weight: 600; overflow-wrap: anywhere; }
.chat-search-meta { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); color: var(--fg2); font-size: var(--text-sm); }
.chat-search-chip { padding: 0 var(--space-1); color: var(--fg2); border: 1px solid var(--border-strong); border-radius: var(--radius-xs); font-family: var(--font-mono); font-size: var(--text-xs); line-height: 16px; }
.chat-search-snippet { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; overflow-wrap: anywhere; color: var(--fg2); font-size: var(--text-base); line-height: 1.45; }
.chat-search-mark { padding: 0 1px; color: var(--fg); background: color-mix(in srgb, var(--accent) 30%, transparent); border-radius: 2px; }
.chat-search-actions { display: flex; justify-content: flex-end; margin-top: var(--space-3); }
.chat-search-close, .chat-search-more { min-height: 44px; padding: 0 var(--space-3); color: inherit; background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); }
.chat-search :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (max-width: 600px) { .chat-search { inset: auto 0 0; transform: none; width: auto; max-height: 90dvh; border-radius: var(--radius) var(--radius) 0 0; padding: var(--space-4) max(var(--space-4), var(--safe-left)) max(var(--space-4), env(safe-area-inset-bottom)) max(var(--space-4), var(--safe-right)); } }
</style>
