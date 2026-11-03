<template>
  <div
    class="title-rail"
    role="group"
    :aria-label="`Chats in this workspace, ${neighbours.index + 1} of ${items.length}`"
  >
    <!-- One chat fills the strip; the neighbours sit off-screen and only slide in
         while a swipe or a step is under way, so the title keeps its place. -->
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
        v-for="(entry, at) in items"
        :key="entry.chatId"
        type="button"
        class="title-rail-item"
        :aria-label="railItemLabel(entry)"
        :aria-current="entry.chatId === currentId ? 'page' : undefined"
        :tabindex="entry.chatId === currentId ? 0 : -1"
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
        <!-- Each title carries its own position, so the dots move with the slide. -->
        <span class="title-rail-meta">
          <span class="title-rail-project">{{ entry.project }}</span>
          <span class="title-rail-dots" aria-hidden="true">
            <template v-if="items.length <= RAIL_MAX_DOTS">
              <span
                v-for="(other, i) in items"
                :key="other.chatId"
                class="title-rail-dot"
                :class="{ 'is-current': i === at, 'is-needs': other.tier === 'needsYou' }"
              />
            </template>
            <span v-else class="title-rail-count">{{ at + 1 }} / {{ items.length }}</span>
          </span>
        </span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  RAIL_MAX_DOTS,
  RAIL_SETTLE_MS,
  RAIL_SLIDE_MS,
  railItemLabel,
  railNeighbours,
  settledIndex,
  type ChatRailItem,
} from '../lib/chatRail'

const props = defineProps<{ items: ChatRailItem[]; currentId: string }>()
const emit = defineEmits<{ select: [chatId: string]; rename: [] }>()

const neighbours = computed(() => railNeighbours(props.items.map(i => i.chatId), props.currentId))

function onItemDblclick(chatId: string): void {
  if (chatId === props.currentId) emit('rename')
}

// The arrow keys on the focused title step to the neighbour, with the same slide as a swipe.
function onItemKeydown(e: KeyboardEvent, chatId: string): void {
  if (chatId !== props.currentId) return
  // Browser chords (Alt+ArrowLeft is Back) are not rail gestures.
  if (e.altKey || e.ctrlKey || e.metaKey) return
  const direction = e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0
  if (!direction) return
  if (slide(direction)) e.preventDefault()
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

// Instant, no animation: puts the current chat in view.
function centre(): void {
  const el = strip.value
  const cur = itemEls()[neighbours.value.index]
  if (el && cur) el.scrollLeft = cur.offsetLeft + cur.offsetWidth / 2 - el.clientWidth / 2
}

/**
 * Slide the neighbour in, then switch to it: the keyboard path (arrow keys, the
 * previous/next chat shortcut) gets the same motion as a swipe. With reduced
 * motion the switch is immediate. Returns false when there is no neighbour.
 */
let sliding: ReturnType<typeof setTimeout> | null = null
function slide(direction: -1 | 1): boolean {
  const target = direction < 0 ? neighbours.value.previous : neighbours.value.next
  if (!target || sliding) return false
  const el = strip.value
  const to = itemEls()[neighbours.value.index + direction]
  const reduce = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (!el || !to || reduce) {
    emit('select', target)
    return true
  }
  gestureSeen = false
  el.scrollTo({ left: to.offsetLeft + to.offsetWidth / 2 - el.clientWidth / 2, behavior: 'smooth' })
  sliding = setTimeout(() => { sliding = null; emit('select', target) }, RAIL_SLIDE_MS)
  return true
}

defineExpose({ slide })

function settle(): void {
  timer = null
  if (sliding || touchActive || !gestureSeen || !strip.value) return
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
  if (sliding) clearTimeout(sliding)
  sliding = null
  if (timer) clearTimeout(timer)
  timer = null
})
</script>

<style scoped src="./titleShimmer.css"></style>

<style scoped>
/* The title sits where the plain header puts it (left, next to close), and the
   position dots follow the project name on its line, so the rail is no taller
   than the title and project it shows. */
.title-rail { display: flex; flex-direction: column; justify-content: center; min-width: 0; flex: 1 1 auto; }
.title-rail-strip { position: relative; display: flex; overflow-x: auto; overflow-y: hidden; scroll-snap-type: x mandatory;
  overscroll-behavior-x: contain; touch-action: pan-x; scrollbar-width: none; min-width: 0; }
.title-rail-strip::-webkit-scrollbar { display: none; }
.title-rail-item { flex: 0 0 100%; scroll-snap-align: start; scroll-snap-stop: always; min-height: var(--touch); min-width: 0;
  padding: 0; display: flex; flex-direction: column; justify-content: center; border: 0; background: none; color: var(--fg);
  font: inherit; text-align: left; cursor: default; }
.title-rail-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-xs); }
.title-rail-title { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: var(--text-lg); font-weight: 600; line-height: 1.25; }
.title-rail-meta { display: flex; align-items: center; gap: 8px; min-width: 0; }
.title-rail-project { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font: var(--text-xs)/1.3 var(--font-mono); color: var(--fg3); }
.title-rail-dots { flex: none; display: flex; align-items: center; gap: 4px; }
.title-rail-dot { width: 4px; height: 4px; border-radius: 50%; background: var(--fg3); opacity: .5; }
.title-rail-dot.is-current { background: var(--fg); opacity: 1; }
.title-rail-dot.is-needs { background: var(--accent); opacity: 1; }
.title-rail-count { font: var(--text-xs)/1 var(--font-mono); color: var(--fg3); }
</style>
