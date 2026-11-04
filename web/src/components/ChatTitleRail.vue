<template>
  <div
    ref="root"
    class="title-rail"
    :class="{ 'title-rail--settled': !moving }"
    :style="{ paddingLeft: `${pad.left}px`, paddingRight: `${pad.right}px`, '--pad-l': `${pad.left}px`, '--pad-r': `${pad.right}px` }"
    role="group"
    :aria-label="`Chats in this workspace, ${neighbours.index + 1} of ${items.length}`"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointercancel="onPointerCancel"
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
      @touchcancel.passive="onTouchCancel"
    >
      <button
        v-for="entry in items"
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
        <span class="title-rail-project">{{ entry.project }}</span>
      </button>
    </div>

    <!-- One segment per chat on the header's bottom edge, under the title. The bright
         one follows the strip's scroll, so it moves with a finger mid-swipe. -->
    <div
      class="title-rail-track"
      aria-hidden="true"
      :style="{ '--n': items.length, '--pos': position, ...(trackDrop === null ? {} : { bottom: `${-trackDrop}px` }) }"
    >
      <span
        v-for="entry in items"
        :key="entry.chatId"
        class="title-rail-seg"
        :class="SEG_STATE[entry.tier] && `title-rail-seg--${SEG_STATE[entry.tier]}`"
      />
      <span class="title-rail-seg-current" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  RAIL_SETTLE_MS,
  RAIL_SLIDE_MS,
  railItemLabel,
  railNeighbours,
  settledIndex,
  type ChatRailItem,
} from '../lib/chatRail'

const props = defineProps<{ items: ChatRailItem[]; currentId: string }>()

// Each segment shows its chat's state with the app's marks in miniature, told apart
// by shape and motion (DESIGN.md "Activity glyphs"): needs you is the alert
// triangle, working the chase, unread the news dot; a quiet chat is a plain segment.
const SEG_STATE: Partial<Record<ChatRailItem['tier'], 'needs' | 'working' | 'unread'>> = {
  needsYou: 'needs',
  working: 'working',
  unread: 'unread',
}
const emit = defineEmits<{
  select: [chatId: string]
  rename: []
  // A downward drag on the title: how far, in px, so the chat can follow the finger.
  pull: [dy: number]
  // The drag ended: the distance and speed (px per ms) decide close or spring back.
  pullEnd: [dy: number, velocity: number]
  // How far the strip sits from the current chat (-1 to 1) mid-swipe, so the
  // transcript can lean the same way.
  lean: [fraction: number]
}>()

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
const root = ref<HTMLElement | null>(null)
// How far the header's bottom edge sits below the rail. The header is 61px on desktop
// and taller on a phone (safe area, 44px targets), so it is measured, not assumed.
const trackDrop = ref<number | null>(null)
// The title centres on the chat's reading column, not on the header: close sits
// before the rail and the header actions after it, and Chat details can take the
// pane's right side. Padding the rail's longer side moves its centre onto the
// column's; the rail never grows, so the title only gives up room, never overlaps.
const pad = ref({ left: 0, right: 0 })
function readingColumn(): HTMLElement | null {
  return root.value?.closest('.chat-panel')?.querySelector<HTMLElement>('.messages-content') ?? null
}
function measureTrack(): void {
  const rail = root.value
  const column = readingColumn()
  if (rail && column) {
    const r = rail.getBoundingClientRect()
    const c = column.getBoundingClientRect()
    // The column may be mid-lean or mid-arrival (a translateX on .chat-column);
    // measure where it rests, not where the animation has it right now.
    const moving = column.closest<HTMLElement>('.chat-column')
    const shift = moving && typeof DOMMatrixReadOnly === 'function'
      ? new DOMMatrixReadOnly(getComputedStyle(moving).transform === 'none' ? undefined : getComputedStyle(moving).transform).m41
      : 0
    const centre = c.left - shift + c.width / 2
    const before = centre - r.left
    const after = r.right - centre
    pad.value = before > 0 && after > 0
      ? { left: Math.max(0, before - after), right: Math.max(0, after - before) }
      : { left: 0, right: 0 }
  }
  const header = rail?.closest('.pane-header')
  if (!rail || !header) { trackDrop.value = null; return }
  trackDrop.value = header.getBoundingClientRect().bottom - rail.getBoundingClientRect().bottom
}
let headerObserver: ResizeObserver | null = null
// Fractional index the strip shows (1.4 = forty percent of the way to the third chat).
const position = ref(neighbours.value.index)
// The track's highlight animates on its own only when the strip is at rest; mid-swipe it follows the scroll.
const moving = ref(false)
function trackPosition(): void {
  const el = strip.value
  position.value = el && el.clientWidth ? el.scrollLeft / el.clientWidth : neighbours.value.index
  emit('lean', Math.max(-1, Math.min(1, position.value - neighbours.value.index)))
}
let touchActive = false
let gestureSeen = false
let nativeTouchPan = false
let touchCanceled = false
let timer: ReturnType<typeof setTimeout> | null = null

function itemEls(): HTMLElement[] {
  return Array.from(strip.value?.querySelectorAll<HTMLElement>('.title-rail-item') ?? [])
}

// Instant, no animation: puts the current chat in view.
function centre(): void {
  const el = strip.value
  const cur = itemEls()[neighbours.value.index]
  if (el && cur) el.scrollLeft = cur.offsetLeft + cur.offsetWidth / 2 - el.clientWidth / 2
  position.value = neighbours.value.index
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
  moving.value = false
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
  moving.value = true
  trackPosition()
  if (timer) clearTimeout(timer)
  timer = setTimeout(settle, RAIL_SETTLE_MS)
}

// Only a sideways wheel is a strip gesture; a vertical one is the page scrolling.
function onWheel(e: WheelEvent): void {
  if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) gestureSeen = true
}

function onTouchStart(): void {
  if (!touchActive) touchCanceled = false
  touchActive = true
  if (!touchCanceled) gestureSeen = true
}

function onTouchEnd(e: TouchEvent): void {
  if (e.touches?.length) return
  touchActive = false
  nativeTouchPan = false
  if (touchCanceled) { touchCanceled = false; return }
  onScroll()
}

function onTouchCancel(e: TouchEvent): void {
  touchCanceled = true
  touchActive = !!e.touches?.length
  nativeTouchPan = touchActive
  gestureSeen = false
  if (timer) { clearTimeout(timer); timer = null }
  moving.value = false
  if (drag?.axis === 'y') emit('pullEnd', 0, 0)
  drag = null
  restoreCurrentTitle()
}

watch(
  () => [props.currentId, props.items.length],
  () => { void nextTick(centre) },
  { flush: 'post' },
)

/**
 * Pointer gestures on the title. After 8px the drag locks to an axis:
 * - down: the chat follows the finger (`pull`), and on release it closes or springs back (`pullEnd`);
 * - sideways with a mouse: the strip follows the pointer like a finger (touch already
 *   scrolls the strip natively), then settles on the neighbour or back.
 * An upward drag does nothing, so the page keeps its own scrolling.
 */
const PULL_LOCK_PX = 8
let drag: { id: number; x0: number; y0: number; t0: number; left0: number; axis: 'x' | 'y' | 'native-x' | 'none' | null; mouse: boolean } | null = null
function onPointerDown(e: PointerEvent): void {
  if (e.button !== 0 || sliding || drag || nativeTouchPan) return
  drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, t0: performance.now(), left0: strip.value?.scrollLeft ?? 0, axis: null, mouse: e.pointerType === 'mouse' }
}
function onPointerMove(e: PointerEvent): void {
  if (!drag || e.pointerId !== drag.id) return
  const dx = e.clientX - drag.x0
  const dy = e.clientY - drag.y0
  if (!drag.axis) {
    if (Math.hypot(dx, dy) < PULL_LOCK_PX) return
    drag.axis = Math.abs(dy) > Math.abs(dx) ? (dy > 0 ? 'y' : 'none') : (drag.mouse ? 'x' : 'native-x')
    if (drag.axis === 'x' || drag.axis === 'y') root.value?.setPointerCapture(drag.id)
    if (drag.axis === 'x' && strip.value) strip.value.style.scrollSnapType = 'none'
  }
  if (drag.axis === 'y') {
    emit('pull', Math.max(0, dy))
  } else if (drag.axis === 'x' && strip.value) {
    strip.value.scrollLeft = drag.left0 - dx
    onScroll()
  }
}
function onPointerUp(e: PointerEvent): void {
  if (!drag || e.pointerId !== drag.id) return
  const d = drag
  drag = null
  const dx = e.clientX - d.x0
  const dy = e.clientY - d.y0
  const velocity = (d.axis === 'y' ? dy : Math.abs(dx)) / Math.max(1, performance.now() - d.t0)
  if (d.axis === 'y') emit('pullEnd', Math.max(0, dy), velocity)
  if (d.axis === 'x' && strip.value) {
    strip.value.style.scrollSnapType = ''
    const width = strip.value.clientWidth || 1
    const passed = Math.abs(dx) > width * 0.25 || (velocity > 0.45 && Math.abs(dx) > 30)
    const direction = dx < 0 ? 1 : -1
    if (!passed || !slide(direction)) {
      const cur = itemEls()[neighbours.value.index]
      strip.value.scrollTo({ left: cur ? cur.offsetLeft : 0, behavior: 'smooth' })
    }
  }
}

function onPointerCancel(e: PointerEvent): void {
  if (!drag || e.pointerId !== drag.id) return
  const d = drag
  drag = null
  // On touch, the browser cancels pointer events when it takes over the
  // strip's native horizontal pan. Let touchend and scroll settling finish it.
  if (!d.mouse && (d.axis === 'native-x' || (d.axis === null && touchActive))) {
    nativeTouchPan = true
    return
  }
  touchActive = false
  gestureSeen = false
  if (timer) { clearTimeout(timer); timer = null }
  moving.value = false
  if (d.axis === 'y') emit('pullEnd', 0, 0)
  if (d.axis !== 'y') restoreCurrentTitle()
}

function restoreCurrentTitle(): void {
  if (!strip.value) return
  strip.value.style.scrollSnapType = ''
  const cur = itemEls()[neighbours.value.index]
  strip.value.scrollTo({ left: cur ? cur.offsetLeft + cur.offsetWidth / 2 - strip.value.clientWidth / 2 : 0, behavior: 'smooth' })
  emit('lean', 0)
}

onMounted(() => {
  centre()
  measureTrack()
  const header = root.value?.closest('.pane-header')
  if (header && typeof ResizeObserver === 'function') {
    headerObserver = new ResizeObserver(measureTrack)
    headerObserver.observe(header)
    // The column moves when Chat details opens or closes, without resizing the header.
    const column = readingColumn()
    if (column) headerObserver.observe(column)
  }
})

onBeforeUnmount(() => {
  headerObserver?.disconnect()
  headerObserver = null
  if (sliding) clearTimeout(sliding)
  sliding = null
  if (timer) clearTimeout(timer)
  timer = null
})
</script>

<style scoped src="./titleShimmer.css"></style>

<style scoped>
/* The current title is centred in the space between close and the header actions,
   with its project under it. The position is a segmented track on the header's
   bottom edge, so nothing but the title and project sits on the text lines. */
.title-rail { touch-action: pan-x; position: relative; display: flex; flex-direction: column; justify-content: center; min-width: 0; flex: 1 1 auto; }
.title-rail-strip { position: relative; display: flex; overflow-x: auto; overflow-y: hidden; scroll-snap-type: x mandatory;
  overscroll-behavior-x: contain; touch-action: pan-x; scrollbar-width: none; min-width: 0; }
.title-rail-strip::-webkit-scrollbar { display: none; }
.title-rail-item { flex: 0 0 100%; scroll-snap-align: start; scroll-snap-stop: always; min-height: var(--touch); min-width: 0;
  padding: 0 8px; display: flex; flex-direction: column; align-items: center; justify-content: center; border: 0; background: none; color: var(--fg);
  font: inherit; text-align: center; cursor: default; }
.title-rail-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-xs); }
.title-rail-title { flex: none; display: block; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: var(--text-lg); font-weight: 600; line-height: 1.25; }
/* The shared header shimmer sizes itself with `flex-basis: 10rem` for a row; the rail's
   items are columns, where that basis is a height. Keep it a 10rem by 12px bar on the
   title line. */
.title-rail-item .chat-title-shimmer { flex: none; width: 10rem; max-width: 100%; height: 12px; margin-block: calc((var(--text-lg) * 1.25 - 12px) / 2); }
.title-rail-project { flex: none; display: block; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font: var(--text-xs)/1.3 var(--font-mono); color: var(--fg3); }
/* On the header's bottom edge: the measured drop sets `bottom`; this fallback is the
   61px desktop header with the rail centred in it. */
.title-rail-track { --gap: 3px; position: absolute; left: calc(var(--pad-l, 0px) + (100% - var(--pad-l, 0px) - var(--pad-r, 0px)) / 2); bottom: calc((var(--touch) - 61px) / 2); transform: translateX(-50%);
  width: min(60%, calc(var(--n) * 31px)); height: 2px; display: flex; gap: var(--gap); pointer-events: none; }
.title-rail-seg { flex: 1 1 0; min-width: 4px; height: 2px; border-radius: 1px; background: var(--border-strong); }
.title-rail-seg-current { position: absolute; top: 0; left: 0; height: 2px; border-radius: 1px; background: var(--fg);
  width: calc((100% - (var(--n) - 1) * var(--gap)) / var(--n));
  transform: translateX(calc(var(--pos) * (100% + var(--gap)))); }
.title-rail-seg { position: relative; }
/* Marks sit just above their segment, inside the header's bottom padding, at most 4px tall, clear of the project line. */
.title-rail-seg--unread::after { content: ""; position: absolute; left: 50%; bottom: 3px; width: 3px; height: 3px; margin-left: -1.5px; border-radius: 50%; background: var(--fg2); }
.title-rail-seg--needs::after { content: ""; position: absolute; left: 50%; bottom: 3px; width: 0; height: 0; margin-left: -3px;
  border-left: 3px solid transparent; border-right: 3px solid transparent; border-bottom: 4px solid var(--accent); }
.title-rail-seg--working { background: linear-gradient(90deg, var(--border-strong) 0 35%, var(--fg2) 45% 55%, var(--border-strong) 65% 100%);
  background-size: 300% 100%; animation: title-rail-chase 1.4s linear infinite; }
@keyframes title-rail-chase { from { background-position: 100% 0; } to { background-position: 0 0; } }
.title-rail--settled .title-rail-seg-current { transition: transform 220ms var(--ease); }
@media (prefers-reduced-motion: reduce) {
  .title-rail--settled .title-rail-seg-current { transition: none; }
  /* Still, but still a different shape: a dashed segment. */
  .title-rail-seg--working { animation: none; background: repeating-linear-gradient(90deg, var(--fg3) 0 4px, transparent 4px 7px); }
}
</style>
