<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useNotificationInboxStore } from '../stores/notificationInbox'
import NotificationInboxList from './NotificationInboxList.vue'
import { useInboxOpenRequests } from '../composables/useInboxPopover'

defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<{ variant?: 'popover' | 'sheet' }>(), { variant: 'popover' })
const emit = defineEmits<{ navigate: [] }>()

const inbox = useNotificationInboxStore()

const isOpen = ref(false)
const buttonEl = ref<HTMLButtonElement | null>(null)
const dialogEl = ref<HTMLElement | null>(null)
const headingEl = ref<HTMLElement | null>(null)
const pos = ref({ top: 0, left: 0 })

const partial = computed(() => inbox.overall === 'stale' || inbox.overall === 'unavailable')

const GAP = 8
const WIDTH = 380

// The popover sits beside the button (the sidebar's right edge), clamped so it
// never leaves the viewport; the sheet hangs full width under the pane header.
function place() {
  const rect = buttonEl.value?.getBoundingClientRect()
  if (!rect) return
  // A bell hidden by a breakpoint (the header bell past 768px) has no box: its
  // open dialog has nothing to point at, so it closes. Opening calls this before
  // `isOpen` is set, so it never trips here.
  if (isOpen.value && rect.width === 0 && rect.height === 0) {
    close()
    return
  }
  if (props.variant === 'sheet') {
    pos.value = { top: rect.bottom + 6, left: GAP }
    return
  }
  const width = Math.min(WIDTH, window.innerWidth - 2 * GAP)
  // Beside the sidebar, not the button, so the collapse control stays reachable.
  const anchor = buttonEl.value?.closest('aside.sidebar')?.getBoundingClientRect() ?? rect
  pos.value = {
    top: Math.max(GAP, rect.top),
    left: Math.max(GAP, Math.min(anchor.right + GAP, window.innerWidth - width - GAP)),
  }
}

const style = computed(() => ({ '--nb-top': `${pos.value.top}px`, '--nb-left': `${pos.value.left}px` }))

function onPointerDown(ev: Event) {
  const target = ev.target as Node | null
  if (!target) return
  if (dialogEl.value?.contains(target) || buttonEl.value?.contains(target)) return
  // The backdrop closes on its own click, so a touch that starts on it does not
  // also land its click on whatever sits underneath.
  if ((target as HTMLElement).classList?.contains('notif-backdrop')) return
  close()
}

function open() {
  if (isOpen.value) return
  place()
  isOpen.value = true
  document.addEventListener('pointerdown', onPointerDown, true)
  window.addEventListener('resize', place)
  void nextTick(() => headingEl.value?.focus())
}

function close(returnFocus = false) {
  if (!isOpen.value) return
  isOpen.value = false
  document.removeEventListener('pointerdown', onPointerDown, true)
  window.removeEventListener('resize', place)
  if (returnFocus) void nextTick(() => buttonEl.value?.focus())
}

function toggle() {
  if (isOpen.value) close(true)
  else open()
}

function onKeydown(ev: KeyboardEvent) {
  if (ev.key !== 'Escape') return
  // ChatLayout's global Escape goes Home; the bell owns Escape while it is open.
  ev.stopPropagation()
  ev.preventDefault()
  close(true)
}

function onFocusout(ev: FocusEvent) {
  const next = ev.relatedTarget as Node | null
  if (!next) return
  if (dialogEl.value?.contains(next) || buttonEl.value?.contains(next)) return
  close()
}

function onNavigate() {
  close()
  emit('navigate')
}

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onPointerDown, true)
  window.removeEventListener('resize', place)
})

// Home's narrow summary asks for the inbox; only the bell that is on screen
// answers (the pane-header one on phones, the sidebar one otherwise).
const openRequests = useInboxOpenRequests()
watch(openRequests, () => {
  if (buttonEl.value?.offsetParent !== null) open()
})

defineExpose({ open, close })
</script>

<template>
  <button
    ref="buttonEl"
    v-bind="$attrs"
    type="button"
    class="notif-bell touch-hit"
    title="Notifications"
    aria-haspopup="dialog"
    :aria-expanded="isOpen ? 'true' : 'false'"
    :aria-label="inbox.accessibleName"
    @click="toggle"
  >
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
      <path d="M6 17V10a6 6 0 0 1 12 0v7" />
      <line x1="4" y1="17" x2="20" y2="17" />
      <line x1="10" y1="21" x2="14" y2="21" />
    </svg>
    <span v-if="inbox.count > 0" class="notif-bell-count" aria-hidden="true">{{ inbox.badgeText }}</span>
    <span v-if="partial" class="notif-bell-ring" aria-hidden="true"></span>
  </button>
  <Teleport to="body">
    <div v-if="isOpen && variant === 'sheet'" class="notif-backdrop" @click="close(true)"></div>
    <div
      v-if="isOpen"
      ref="dialogEl"
      role="dialog"
      aria-label="Notifications"
      class="notif-popover"
      :class="`notif-popover--${variant}`"
      :style="style"
      tabindex="-1"
      @keydown="onKeydown"
      @focusout="onFocusout"
    >
      <header class="notif-popover-head">
        <h2 ref="headingEl" tabindex="-1" class="notif-popover-title">Notifications</h2>
        <span v-if="inbox.count > 0" class="notif-popover-count">{{ inbox.count }}</span>
        <button
          type="button"
          class="notif-popover-close"
          aria-label="Close notifications"
          @click="close(true)"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="square" aria-hidden="true">
            <line x1="6" y1="6" x2="18" y2="18" />
            <line x1="18" y1="6" x2="6" y2="18" />
          </svg>
        </button>
      </header>
      <div class="notif-popover-body">
        <NotificationInboxList :density="variant" @navigate="onNavigate" />
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.notif-bell {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--fg2);
  cursor: pointer;
  text-decoration: none;
  transition: color 120ms var(--ease);
}
.notif-bell:hover,
.notif-bell[aria-expanded="true"] { color: var(--fg); }
.notif-bell:focus-visible,
.notif-popover-close:focus-visible,
.notif-popover-title:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.notif-bell-count {
  position: absolute;
  top: 2px;
  right: 4px;
  min-width: 14px;
  padding: 1px 3px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  background: var(--bg2);
  color: var(--fg2);
  font: 700 9px/1 var(--font-mono);
  font-variant-numeric: tabular-nums;
  text-align: center;
}

/* Hollow ring: some sources are stale or unavailable, so the count is not the
   whole story. Hollow, never filled, so it cannot read as "needs you". */
.notif-bell-ring {
  position: absolute;
  top: 2px;
  left: 2px;
  box-sizing: border-box;
  width: 8px;
  height: 8px;
  border: 1.5px solid var(--warning);
  border-radius: 50%;
  background: transparent;
}

.notif-backdrop {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: rgb(0 0 0 / 40%);
}

.notif-popover {
  position: fixed;
  top: var(--nb-top);
  left: var(--nb-left);
  z-index: 301;
  box-sizing: border-box;
  width: min(380px, calc(100vw - 16px));
  max-height: min(620px, calc(100dvh - var(--nb-top) - 16px));
  overflow-y: auto;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg);
  box-shadow: 0 1rem 2.5rem rgb(0 0 0 / 38%);
}

.notif-popover:focus { outline: none; }

.notif-popover--sheet {
  right: 8px;
  width: auto;
  max-height: calc(100dvh - var(--nb-top) - 8px);
}

.notif-popover-head {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 8px 10px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
}

.notif-popover-title {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: 650;
}

.notif-popover-count {
  color: var(--fg3);
  font: 600 var(--text-xs) var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.notif-popover-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  margin-left: auto;
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--fg2);
  cursor: pointer;
}
.notif-popover-close:hover { color: var(--fg); }

.notif-popover-body {
  padding: 0 16px 8px;
}

@media (pointer: coarse) {
  .notif-bell,
  .notif-popover-close { width: var(--touch); height: var(--touch); }
}
</style>
