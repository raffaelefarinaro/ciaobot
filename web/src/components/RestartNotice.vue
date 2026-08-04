<template>
  <!-- Deliberately not a full-screen curtain: a restart is a background event,
       so the app stays visible behind this card and the user keeps their
       bearings. Because a fixed card always sits on top of *something*, it is
       draggable — and where the user drops it is remembered for next time. -->
  <div
    ref="cardEl"
    class="restart-card"
    :class="{ 'restart-card--moved': !!pos, 'restart-card--dragging': dragging }"
    :style="cardStyle"
    role="status"
    aria-live="polite"
    tabindex="0"
    aria-label="Restart status. Drag, or use the arrow keys, to move this card."
    title="Drag to move"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="endDrag"
    @pointercancel="endDrag"
    @keydown="onKeydown"
  >
    <div class="restart-head">
      <span class="wordmark wordmark--sm">ciaobot</span>
      <span class="restart-tag">restart</span>
    </div>

    <div class="restart-body">
      <span class="restart-spinner" aria-hidden="true">{{ spinnerFrame }}</span>
      <p class="restart-message">{{ message || DEFAULT_RESTART_MESSAGE }}</p>
    </div>

    <div class="restart-foot">
      <span class="restart-prompt">$</span>
      <span class="restart-prompt-text">waiting for server</span>
      <span class="caret"></span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { DEFAULT_RESTART_MESSAGE } from '../lib/serverRestart'

defineProps<{
  // Already normalised and length-capped by restartMessageForDisplay(); the
  // line clamp below is the visual backstop, not the limit.
  message?: string
}>()

const FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
const frame = ref(0)
const spinnerFrame = ref(FRAMES[0])
let timer: ReturnType<typeof setInterval> | null = null

// ── Dragging ────────────────────────────────────────────────────────────────
// `pos` null means "wherever the stylesheet puts it" (bottom centre). Once the
// user moves the card we switch to explicit left/top pixels.
const POS_KEY = 'ciao:restart-notice-pos'
const EDGE_MARGIN = 8
const KEY_STEP = 16

const cardEl = ref<HTMLElement | null>(null)
const pos = ref<{ x: number; y: number } | null>(loadPos())
const dragging = ref(false)
// Grab offset inside the card, plus the size measured at drag start so a
// mid-drag re-render can't change what we clamp against.
let grabX = 0
let grabY = 0
let cardW = 0
let cardH = 0

const cardStyle = computed(() =>
  pos.value
    ? { left: `${pos.value.x}px`, top: `${pos.value.y}px`, bottom: 'auto', transform: 'none' }
    : undefined,
)

function safeGetItem(key: string): string | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null
  } catch {
    return null
  }
}

function safeSetItem(key: string, value: string) {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(key, value)
  } catch {}
}

function loadPos(): { x: number; y: number } | null {
  const raw = safeGetItem(POS_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    if (typeof parsed?.x === 'number' && typeof parsed?.y === 'number') {
      return { x: parsed.x, y: parsed.y }
    }
  } catch {}
  return null
}

function clamp(value: number, min: number, max: number) {
  // A card taller/wider than the viewport would invert the bounds; pin to min.
  return max < min ? min : Math.min(Math.max(value, min), max)
}

function moveTo(x: number, y: number) {
  pos.value = {
    x: clamp(x, EDGE_MARGIN, window.innerWidth - cardW - EDGE_MARGIN),
    y: clamp(y, EDGE_MARGIN, window.innerHeight - cardH - EDGE_MARGIN),
  }
}

function measureFromDom(): DOMRect | null {
  const rect = cardEl.value?.getBoundingClientRect() ?? null
  if (rect) {
    cardW = rect.width
    cardH = rect.height
  }
  return rect
}

// A remembered position can land off-screen after a window resize, and the
// same applies while the notice is up.
function clampToViewport() {
  if (!pos.value) return
  measureFromDom()
  moveTo(pos.value.x, pos.value.y)
}

function onPointerDown(e: PointerEvent) {
  if (e.pointerType === 'mouse' && e.button !== 0) return
  const rect = measureFromDom()
  if (!rect) return
  grabX = e.clientX - rect.left
  grabY = e.clientY - rect.top
  dragging.value = true
  // Start from the current on-screen box so the first drag out of the default
  // bottom-centre spot doesn't jump.
  pos.value = { x: rect.left, y: rect.top }
  cardEl.value?.setPointerCapture?.(e.pointerId)
}

function onPointerMove(e: PointerEvent) {
  if (!dragging.value) return
  e.preventDefault()
  moveTo(e.clientX - grabX, e.clientY - grabY)
}

function endDrag() {
  if (!dragging.value) return
  dragging.value = false
  if (pos.value) safeSetItem(POS_KEY, JSON.stringify(pos.value))
}

// Keyboard equivalent of the drag, so the card can be pushed off whatever it
// covers without a pointer.
function onKeydown(e: KeyboardEvent) {
  const dx = e.key === 'ArrowLeft' ? -KEY_STEP : e.key === 'ArrowRight' ? KEY_STEP : 0
  const dy = e.key === 'ArrowUp' ? -KEY_STEP : e.key === 'ArrowDown' ? KEY_STEP : 0
  if (!dx && !dy) return
  e.preventDefault()
  const rect = measureFromDom()
  if (!rect) return
  moveTo(rect.left + dx, rect.top + dy)
  if (pos.value) safeSetItem(POS_KEY, JSON.stringify(pos.value))
}

onMounted(() => {
  timer = setInterval(() => {
    frame.value = (frame.value + 1) % FRAMES.length
    spinnerFrame.value = FRAMES[frame.value]
  }, 90)
  clampToViewport()
  window.addEventListener('resize', clampToViewport)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('resize', clampToViewport)
})
</script>

<style scoped>
.restart-card {
  position: fixed;
  bottom: calc(16px + var(--safe-bottom));
  left: 50%;
  transform: translateX(-50%);
  z-index: 300;
  width: min(400px, calc(100vw - 24px));
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: 12px 14px;
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  cursor: grab;
  user-select: none;
  /* Dragging owns the gesture; don't let the page scroll under the finger. */
  touch-action: none;
  animation: restart-in 200ms var(--ease);
}
.restart-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.restart-card--dragging {
  cursor: grabbing;
  box-shadow: 0 14px 36px rgba(0, 0, 0, 0.6);
}
/* Once positioned by hand the entrance slide would fight the inline transform,
   so moved cards just fade in. */
.restart-card--moved {
  animation-name: restart-fade-in;
}

.restart-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
}
.restart-tag {
  font-size: var(--text-xs);
  color: var(--fg3);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

.restart-body {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
}
.restart-spinner {
  color: var(--accent);
  font-size: var(--text-base);
  line-height: 1.4;
  flex-shrink: 0;
}
.restart-message {
  margin: 0;
  min-width: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.4;
  /* A URL or stack frame with no spaces would otherwise widen the card past
     its max-width instead of wrapping. */
  overflow-wrap: anywhere;
  /* Hard stop on height, in case a message arrives longer than we expect. */
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.restart-foot {
  padding-top: var(--space-2);
  border-top: 1px dashed var(--border);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--fg2);
}
.restart-prompt {
  color: var(--accent);
  font-weight: 700;
}
.restart-prompt-text {
  color: var(--fg2);
}

@keyframes restart-in {
  from { opacity: 0; transform: translate(-50%, 10px); }
  to { opacity: 1; transform: translate(-50%, 0); }
}
@keyframes restart-fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .restart-card { animation: none; }
}
</style>
