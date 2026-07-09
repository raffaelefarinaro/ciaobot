<template>
  <div class="toast-stack" v-if="store.toasts.length">
    <div
      v-for="t in store.toasts"
      :key="t.id"
      class="toast"
      :class="{ 'toast-error': t.variant === 'error', 'toast-swiping': swipeId === t.id }"
      :style="swipeId === t.id ? swipeStyle : undefined"
      role="status"
      @click="onClick(t)"
      @pointerdown="onPointerDown(t, $event)"
      @pointermove="onPointerMove($event)"
      @pointerup="onPointerUp(t)"
      @pointercancel="onPointerUp(t)"
    >
      <div class="toast-title">{{ t.title }}</div>
      <div class="toast-body">{{ t.body }}</div>
      <button
        v-if="t.variant === 'error'"
        class="toast-fix"
        @click.stop="onFix(t)"
      >Fix this error</button>
      <button
        class="toast-close"
        @click.stop="store.dismissToast(t.id)"
        aria-label="Dismiss"
      >&times;</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useProjectStore } from '../stores/projects'
import type { InAppToast } from '../lib/types'

const store = useProjectStore()

// ── Swipe-to-dismiss ────────────────────────────────────────────────────────
// Horizontal drag past a threshold dismisses the toast. Tracked per-toast so
// only the one under the finger moves. A movement threshold before we treat a
// gesture as a swipe keeps plain taps (which open the linked chat) working.
const SWIPE_DISMISS_PX = 80
const swipeId = ref<number | null>(null)
const swipeStartX = ref(0)
const swipeDX = ref(0)
const swiping = ref(false)

const swipeStyle = computed(() => ({
  transform: `translateX(${swipeDX.value}px)`,
  opacity: String(Math.max(0, 1 - Math.abs(swipeDX.value) / (SWIPE_DISMISS_PX * 2))),
}))

function onPointerDown(toast: InAppToast, e: PointerEvent) {
  if (e.pointerType === 'mouse' && e.button !== 0) return
  // Capturing the pointer on the toast div also redirects the compatibility
  // `click` event to it, so a press starting on a button (Fix/Close) would
  // never reach the button's own handler. Skip swipe tracking entirely when
  // the gesture starts on a button and let the click behave normally.
  if ((e.target as HTMLElement | null)?.closest('button')) return
  swipeId.value = toast.id
  swipeStartX.value = e.clientX
  swipeDX.value = 0
  swiping.value = false
  ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
}

function onPointerMove(e: PointerEvent) {
  if (swipeId.value === null) return
  swipeDX.value = e.clientX - swipeStartX.value
  if (Math.abs(swipeDX.value) > 6) swiping.value = true
}

function onPointerUp(toast: InAppToast) {
  if (swipeId.value === null) return
  const dismissed = Math.abs(swipeDX.value) >= SWIPE_DISMISS_PX
  swipeId.value = null
  swipeDX.value = 0
  if (dismissed) store.dismissToast(toast.id)
}

async function onClick(toast: InAppToast) {
  // Ignore the click that fires at the end of a swipe gesture.
  if (swiping.value) {
    swiping.value = false
    return
  }
  // Global error toasts aren't tied to a chat — clicking the body is a no-op
  // (use the Fix / dismiss buttons instead).
  if (!toast.chat_id) return
  // Switch workspace if needed (the toast may be for a chat in the other one)
  const project = store.projectFor(toast.chat_id)
  if (project && project.workspace !== store.activeWorkspace) {
    await store.switchWorkspace(project.workspace)
  }
  await store.switchChat(toast.chat_id)
  store.dismissToast(toast.id)
}

async function onFix(toast: InAppToast) {
  store.dismissToast(toast.id)
  try {
    await store.fixError({ errorText: toast.errorText || toast.body, title: 'Fix error' })
  } catch (e) {
    store.pushErrorToast('Could not open fix chat', `${(e as Error)?.message || e}`)
  }
}
</script>

<style scoped>
.toast-stack {
  position: fixed;
  top: calc(12px + var(--safe-top));
  right: calc(12px + var(--safe-right));
  z-index: 200;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(360px, calc(100vw - 24px));
  pointer-events: none;
}

.toast {
  background: var(--bg-elev);
  color: var(--fg);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  padding: 10px 32px 10px 12px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  cursor: pointer;
  position: relative;
  pointer-events: auto;
  animation: toast-in 180ms var(--ease);
  /* Horizontal swipe-to-dismiss; keep vertical scroll gestures to the page. */
  touch-action: pan-y;
  transition: transform 200ms var(--ease), opacity 200ms var(--ease);
}

/* While the finger is down the transform tracks the pointer directly. */
.toast-swiping {
  transition: none;
  cursor: grabbing;
  user-select: none;
}

.toast:hover { background: var(--bg3); }

/* Error variant recolors the existing accent edge to a danger tone and drops
   the pointer cursor since the body isn't clickable. */
.toast-error {
  border-left-color: var(--error);
  cursor: default;
  padding-bottom: 12px;
}

.toast-fix {
  margin-top: 8px;
  padding: 4px 12px;
  background: transparent;
  color: var(--error);
  border: 1px solid var(--error);
  border-radius: var(--radius-sm);
  font-family: var(--font);
  font-size: var(--text-sm);
  cursor: pointer;
}
.toast-fix:hover { background: var(--error); color: var(--bg); }

.toast-title {
  font-size: var(--text-base);
  font-weight: 600;
  margin-bottom: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.toast-body {
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.toast-close {
  position: absolute;
  top: 4px;
  right: 4px;
  background: transparent;
  border: none;
  color: var(--fg2);
  font-size: 18px;
  line-height: 1;
  width: 24px;
  height: 24px;
  cursor: pointer;
  border-radius: var(--radius-sm);
}
.toast-close:hover { color: var(--fg); background: var(--bg2); }

@keyframes toast-in {
  from { opacity: 0; transform: translateY(-8px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 600px) {
  .toast-stack {
    left: calc(12px + var(--safe-left));
    right: calc(12px + var(--safe-right));
    max-width: none;
  }
}
</style>
