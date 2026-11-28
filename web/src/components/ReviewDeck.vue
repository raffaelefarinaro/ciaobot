<template>
  <div class="rvd" :style="{ '--rvd-behind': behind }">
    <!-- The next two items peek out behind the card: shape only, nothing to read. -->
    <div v-for="n in behind" :key="n" class="rvd-shadow" :class="`rvd-shadow--${n}`" aria-hidden="true"></div>
    <article
      ref="card"
      class="rvd-card"
      :class="{ 'rvd-card--dragging': dragging, 'rvd-card--flying': flying, 'rvd-card--approve': progress > 0, 'rvd-card--dismiss': progress < 0 }"
      :style="cardStyle"
      :aria-labelledby="titleId"
      data-review-card
      @pointerdown="onDown"
      @pointermove="onMove"
      @pointerup="onUp"
      @pointercancel="onCancel"
      @click.capture="onClickCapture"
    >
      <span class="rvd-stamp rvd-stamp--approve" :style="{ opacity: Math.max(0, progress) }" aria-hidden="true">APPROVE</span>
      <span class="rvd-stamp rvd-stamp--dismiss" :style="{ opacity: Math.max(0, -progress) }" aria-hidden="true">{{ dismissStamp }}</span>
      <slot />
    </article>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { swipeAxis, swipeOutcome, swipeProgress, swipeTransform, type SwipeOutcome } from '../lib/unsureSwipe'

/**
 * The phone queue (Memory · Unsure, Memory · Skills): the current item as a
 * card (its content is the slot), the next ones peeking behind. Dragging the card sideways tilts it (not under reduced motion) and
 * stamps APPROVE or the dismiss word (DISMISS, or REJECT); let go past the threshold (lib/unsureSwipe.ts)
 * and it flies off and emits `decide`, short of it and it springs back. A
 * vertical move is left to the browser, so a long card still scrolls; a drag
 * never also clicks a link inside the card. `fling(op)` runs the same exit
 * for the buttons under the card and the keys; `reset()` brings the card back
 * when the write was refused.
 */
const props = withDefaults(defineProps<{
  /** The item's identity: a new one brings the card back to rest. */
  itemKey: string
  /** The id of the card's title, which names the card. */
  titleId: string
  /** How many cards peek out behind this one (0 to 2). */
  behind: number
  /** No swipe while a write is in flight or care runs. */
  disabled?: boolean
  /** The stamp a leftward drag shows. */
  dismissStamp?: string
}>(), { disabled: false, dismissStamp: 'DISMISS' })
const emit = defineEmits<{ decide: [op: 'approve' | 'dismiss'] }>()

const card = ref<HTMLElement | null>(null)
const dx = ref(0)
const dragging = ref(false)
const flying = ref(false)
let start: { x: number; y: number; t: number; id: number } | null = null
let axis: boolean | null = null
let dragged = false

function reducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}
function width(): number {
  return card.value?.getBoundingClientRect().width || 360
}

const progress = computed(() => swipeProgress(dx.value, width()))
const cardStyle = computed(() => {
  const transform = swipeTransform(dx.value, width(), reducedMotion())
  return transform ? { transform } : undefined
})

function onDown(event: PointerEvent): void {
  if (props.disabled || flying.value || !event.isPrimary || (event.pointerType === 'mouse' && event.button !== 0)) return
  start = { x: event.clientX, y: event.clientY, t: event.timeStamp, id: event.pointerId }
  axis = null
  dragged = false
}

function onMove(event: PointerEvent): void {
  if (!start || event.pointerId !== start.id) return
  const moveX = event.clientX - start.x
  const moveY = event.clientY - start.y
  if (axis === null) {
    axis = swipeAxis(moveX, moveY)
    if (axis === null) return
    if (!axis) { start = null; return }
    dragging.value = true
    dragged = true
    card.value?.setPointerCapture?.(event.pointerId)
  }
  dx.value = moveX
}

function onUp(event: PointerEvent): void {
  if (!start || event.pointerId !== start.id) return
  const outcome: SwipeOutcome = dragging.value ? swipeOutcome(event.clientX - start.x, event.timeStamp - start.t, width()) : null
  start = null
  dragging.value = false
  if (outcome) void fling(outcome)
  else dx.value = 0
}

function onCancel(): void {
  start = null
  dragging.value = false
  dx.value = 0
}

// A drag ends with a click on whatever was under the finger; it is not one.
function onClickCapture(event: MouseEvent): void {
  if (!dragged) return
  dragged = false
  event.preventDefault()
  event.stopPropagation()
}

/** Send the card off to one side, then emit; under reduced motion, emit at once. */
async function fling(op: 'approve' | 'dismiss'): Promise<void> {
  if (flying.value) return
  if (reducedMotion()) {
    dx.value = 0
    emit('decide', op)
    return
  }
  flying.value = true
  dx.value = (op === 'approve' ? 1 : -1) * Math.round(width() * 1.25)
  await new Promise(resolve => setTimeout(resolve, 180))
  emit('decide', op)
}

/** Bring the card back to rest (a refused write, or a new item). */
function reset(): void {
  flying.value = false
  dragging.value = false
  dx.value = 0
  start = null
}

watch(() => props.itemKey, reset)

defineExpose({ fling, reset })
</script>

<style scoped>
.rvd { position: relative; display: grid; padding-bottom: calc(var(--rvd-behind, 0) * 8px); }
.rvd-shadow,
.rvd-card { grid-area: 1 / 1; }
.rvd-shadow {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg2);
  transform-origin: 50% 100%;
}
.rvd-shadow--1 { transform: translateY(8px) scale(0.96); opacity: 0.7; }
.rvd-shadow--2 { transform: translateY(16px) scale(0.92); opacity: 0.45; }

.rvd-card {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: min(56dvh, 520px);
  max-height: 68dvh;
  overflow-y: auto;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg2);
  box-shadow: 0 12px 32px -14px rgb(0 0 0 / 45%);
  /* Vertical scrolling stays the browser's; sideways is the swipe. */
  touch-action: pan-y;
  /* A sideways drag must not also swipe the browser back or forward. */
  overscroll-behavior-x: none;
  user-select: none;
  -webkit-user-select: none;
  transition: transform 180ms var(--ease), border-color 120ms var(--ease);
}
.rvd-card > :slotted(*) { flex: 1; }
.rvd-card--dragging { transition: none; cursor: grabbing; }
.rvd-card--approve { border-color: var(--success); }
.rvd-card--dismiss { border-color: var(--error); }

.rvd-stamp {
  position: absolute;
  top: 14px;
  z-index: 2;
  padding: 2px 10px;
  border: 2px solid currentColor;
  border-radius: var(--radius);
  background: var(--bg2);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  font-weight: 700;
  letter-spacing: 0.04em;
  pointer-events: none;
}
.rvd-stamp--approve { right: 14px; color: var(--success); transform: rotate(6deg); }
.rvd-stamp--dismiss { left: 14px; color: var(--error-text); transform: rotate(-6deg); }

@media (prefers-reduced-motion: reduce) {
  .rvd-card { transition: none; }
  .rvd-stamp { transform: none; }
}
</style>
