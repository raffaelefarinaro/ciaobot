<template>
  <div
    class="title-rail"
    :class="{ 'title-rail--count': items.length > RAIL_MAX_DOTS }"
    role="group"
    :aria-label="`Chats in this workspace, ${neighbours.index + 1} of ${items.length}`"
  >
    <button
      type="button"
      class="btn-icon title-rail-arrow title-rail-arrow--prev"
      aria-label="Previous chat"
      :title="neighbours.previous ? titleOf(neighbours.previous) : undefined"
      :disabled="!neighbours.previous"
      @click="neighbours.previous && emit('select', neighbours.previous)"
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="15 18 9 12 15 6"/>
      </svg>
    </button>

    <div
      ref="strip"
      class="title-rail-strip"
      @scroll.passive="onScroll"
      @wheel.passive="onWheel"
      @touchstart.passive="onTouchStart"
      @touchend.passive="onTouchEnd"
      @touchcancel.passive="onTouchEnd"
    >
      <button
        v-for="(entry, i) in items"
        :key="entry.chatId"
        type="button"
        class="title-rail-item"
        :class="{ 'is-before': i < neighbours.index, 'is-after': i > neighbours.index }"
        :aria-label="railItemLabel(entry)"
        :aria-current="entry.chatId === currentId ? 'page' : undefined"
        :tabindex="entry.chatId === currentId ? 0 : -1"
        @click="onItemClick(entry.chatId)"
        @dblclick="onItemDblclick(entry.chatId)"
        @keydown="onItemKeydown($event, entry.chatId)"
      >
        <span
          v-if="entry.titlePending"
          class="chat-title-shimmer"
          aria-hidden="true"
        />
        <span
          v-else
          class="title-rail-title"
          :class="{ 'pane-title chat-title': entry.chatId === currentId }"
          :title="entry.title"
        >{{ entry.title }}</span>
        <span class="title-rail-project">{{ entry.project }}</span>
        <span class="title-rail-signals" aria-hidden="true">
          <ChatSignals :chat-id="entry.chatId" density="row" />
        </span>
      </button>
    </div>

    <button
      type="button"
      class="btn-icon title-rail-arrow title-rail-arrow--next"
      aria-label="Next chat"
      :title="neighbours.next ? titleOf(neighbours.next) : undefined"
      :disabled="!neighbours.next"
      @click="neighbours.next && emit('select', neighbours.next)"
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="9 18 15 12 9 6"/>
      </svg>
    </button>

    <div class="title-rail-dots" aria-hidden="true">
      <template v-if="items.length <= RAIL_MAX_DOTS">
        <span
          v-for="(entry, i) in items"
          :key="entry.chatId"
          class="title-rail-dot"
          :class="{ 'is-current': i === neighbours.index, 'is-needs': entry.tier === 'needsYou' }"
        />
      </template>
      <span v-else class="title-rail-count">{{ neighbours.index + 1 }} / {{ items.length }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ChatSignals from './ChatSignals.vue'
import {
  RAIL_MAX_DOTS,
  RAIL_SETTLE_MS,
  railItemLabel,
  railNeighbours,
  settledIndex,
  type ChatRailItem,
} from '../lib/chatRail'

const props = defineProps<{ items: ChatRailItem[]; currentId: string }>()
const emit = defineEmits<{ select: [chatId: string]; rename: [] }>()

const neighbours = computed(() => railNeighbours(props.items.map(i => i.chatId), props.currentId))

function titleOf(chatId: string): string {
  return props.items.find(i => i.chatId === chatId)?.title ?? ''
}

function onItemClick(chatId: string): void {
  if (chatId !== props.currentId) emit('select', chatId)
}

function onItemDblclick(chatId: string): void {
  if (chatId === props.currentId) emit('rename')
}

// Keyboard path for phones, where the arrow buttons are hidden: the arrow keys step from the current item.
function onItemKeydown(e: KeyboardEvent, chatId: string): void {
  if (chatId !== props.currentId) return
  const target = e.key === 'ArrowLeft' ? neighbours.value.previous : e.key === 'ArrowRight' ? neighbours.value.next : null
  if (!target) return
  e.preventDefault()
  emit('select', target)
}

// Settle logic: a neighbour is chosen only after a real finger or wheel gesture.
// Programmatic centring, focus-induced scrolling and screen-reader scrolling
// move the strip without a gesture, so they never switch the chat.
const strip = ref<HTMLElement | null>(null)
let touchActive = false
let gestureSeen = false
let timer: ReturnType<typeof setTimeout> | null = null

function itemEls(): HTMLElement[] {
  return Array.from(strip.value?.querySelectorAll<HTMLElement>('.title-rail-item') ?? [])
}

// Instant, no animation: puts the current chat in the middle of the strip.
function centre(): void {
  const el = strip.value
  const cur = itemEls()[neighbours.value.index]
  if (el && cur) el.scrollLeft = cur.offsetLeft + cur.offsetWidth / 2 - el.clientWidth / 2
}

function settle(): void {
  timer = null
  if (touchActive || !gestureSeen || !strip.value) return
  gestureSeen = false
  const i = settledIndex(
    strip.value.scrollLeft + strip.value.clientWidth / 2,
    itemEls().map(e => e.offsetLeft + e.offsetWidth / 2),
  )
  const target = props.items[i]
  if (target && target.chatId !== props.currentId) emit('select', target.chatId)
}

function onScroll(): void {
  if (timer) clearTimeout(timer)
  timer = setTimeout(settle, RAIL_SETTLE_MS)
}

// Only a sideways wheel is a strip gesture; a vertical one is the page scrolling.
function onWheel(e: WheelEvent): void {
  if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) gestureSeen = true
}

function onTouchStart(): void {
  touchActive = true
  gestureSeen = true
}

function onTouchEnd(): void {
  touchActive = false
  onScroll()
}

watch(
  () => [props.currentId, props.items.length],
  () => { void nextTick(centre) },
  { flush: 'post' },
)

onMounted(centre)

onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
  timer = null
})
</script>

<style scoped src="./titleShimmer.css"></style>

<style scoped>
/* Title line centre below the rail's top edge: items start at the top, so it is half the title line box. ChatPanel uses the same value. */
.title-rail { --rail-item: 72%; --rail-title-centre: calc(var(--text-lg) * 0.625); display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; min-width: 0; flex: 1 1 auto; position: relative; }
.title-rail-strip { position: relative; display: flex; overflow-x: auto; overflow-y: hidden; scroll-snap-type: x mandatory;
  overscroll-behavior-x: contain; touch-action: pan-x; scrollbar-width: none; padding-inline: calc((100% - var(--rail-item)) / 2); grid-column: 2; grid-row: 1; min-width: 0; }
.title-rail-strip::-webkit-scrollbar { display: none; }
.title-rail-item { flex: 0 0 var(--rail-item); scroll-snap-align: center; scroll-snap-stop: always; min-height: var(--touch);
  padding: 0 8px 8px; display: flex; flex-direction: column; justify-content: flex-start; border: 0; background: none; color: var(--fg);
  font: inherit; text-align: center; opacity: .45; transform: scale(.9); transition: opacity 120ms var(--ease), transform 120ms var(--ease); cursor: pointer; min-width: 0; }
.title-rail-item[aria-current="page"] { opacity: 1; transform: none; cursor: default; }
/* Peeking edges show the words nearest the centre. */
.title-rail-item.is-before { text-align: right; }
.title-rail-item.is-after { text-align: left; }
.title-rail-title { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: var(--text-lg); font-weight: 600; line-height: 1.25; }
.title-rail-project { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font: var(--text-xs)/1.3 var(--font-mono); color: var(--fg3); }
.title-rail-signals { display: inline-flex; }
.title-rail-dots { grid-column: 2; grid-row: 1; align-self: end; justify-self: center; display: flex; gap: 4px; pointer-events: none; margin-bottom: 2px; }
.title-rail-dot { width: 4px; height: 4px; border-radius: 50%; background: var(--fg3); opacity: .5; }
.title-rail-dot.is-current { background: var(--fg); opacity: 1; }
.title-rail-dot.is-needs { background: var(--accent); opacity: 1; }
.title-rail--count .title-rail-item { padding-bottom: 16px; }
.title-rail-count { font: var(--text-xs)/1 var(--font-mono); color: var(--fg3); }
/* A .btn-icon box is 46px: 44px plus its 1px border. The selector outranks PaneHeader's `.pane-header .btn-icon` margin, which would otherwise pull the arrow up 7px. */
.title-rail > .title-rail-arrow.btn-icon { grid-row: 1; align-self: start; margin-top: calc(var(--rail-title-centre) - 23px); }
.title-rail-arrow--prev { grid-column: 1; }
.title-rail-arrow--next { grid-column: 3; }
.title-rail-arrow:disabled { opacity: .35; cursor: default; }
@media (max-width: 767px) { .title-rail-arrow { display: none; } .title-rail { --rail-item: 80%; } }
@media (prefers-reduced-motion: reduce) { .title-rail-item { transition: none; } }
</style>
