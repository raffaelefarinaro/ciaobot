<template>
  <div class="chat-more">
    <DropdownMenuRoot v-model:open="menuOpen" :modal="false">
      <DropdownMenuTrigger as-child>
        <button
          ref="triggerEl"
          type="button"
          class="btn-icon chat-more-btn"
          :aria-label="triggerLabel"
          title="More chat actions"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg>
          <span v-if="inbox.count > 0" class="chat-more-dot" aria-hidden="true"></span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuPortal>
        <!-- as-child, with the menu box in this template: the scoped styles only
             reach elements rendered here, not the popper wrapper reka builds. -->
        <DropdownMenuContent
          as-child
          align="end"
          :side-offset="6"
          :collision-padding="8"
          @close-auto-focus="onCloseAutoFocus"
        >
          <div class="chat-more-menu">
            <DropdownMenuItem as-child @select="emit('details')">
              <button type="button">Chat details</button>
            </DropdownMenuItem>
            <DropdownMenuItem as-child @select="requestSheet">
              <button type="button">
                <span>Notifications</span>
                <span v-if="inbox.count > 0" class="chat-more-count" aria-hidden="true">{{ inbox.badgeText }}</span>
              </button>
            </DropdownMenuItem>
            <DropdownMenuItem v-if="canArchive" as-child @select="emit('archive')">
              <button type="button">Archive</button>
            </DropdownMenuItem>
            <template v-if="historyNav.canBack.value">
              <DropdownMenuSeparator class="chat-more-sep" />
              <DropdownMenuItem as-child @select="historyNav.back">
                <button type="button">{{ historyNav.backLabel.value }}</button>
              </DropdownMenuItem>
            </template>
          </div>
        </DropdownMenuContent>
      </DropdownMenuPortal>
    </DropdownMenuRoot>
    <!-- The sheet has no button of its own here: the ⋯ trigger is its anchor. -->
    <NotificationBell ref="bell" :trigger="false" variant="sheet" />
  </div>
</template>

<script setup lang="ts">
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuRoot,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from 'reka-ui'
import { computed, nextTick, ref } from 'vue'
import NotificationBell from './NotificationBell.vue'
import { useNotificationInboxStore } from '../stores/notificationInbox'
import { useHistoryNav } from '../composables/useHistoryNav'

defineProps<{ canArchive: boolean }>()
const emit = defineEmits<{ details: []; archive: [] }>()

const inbox = useNotificationInboxStore()
const historyNav = useHistoryNav()

const menuOpen = ref(false)
const triggerEl = ref<HTMLButtonElement | null>(null)
const bell = ref<InstanceType<typeof NotificationBell> | null>(null)
let sheetRequested = false

const triggerLabel = computed(() => (
  inbox.count > 0 ? `More chat actions, ${inbox.count} unread notifications` : 'More chat actions'
))

// Choosing Notifications closes the menu. Reka would then return focus to the
// trigger, which would take it off the sheet that opens next, so that focus
// return is suppressed and the sheet takes focus itself.
function requestSheet() {
  sheetRequested = true
}

function onCloseAutoFocus(event: Event) {
  if (!sheetRequested) return
  sheetRequested = false
  event.preventDefault()
  void nextTick(() => bell.value?.open(triggerEl.value ?? undefined))
}

function focusTrigger() {
  triggerEl.value?.focus()
}

defineExpose({ focusTrigger })
</script>

<style scoped>
/* The dot hangs off the glyph's top-right corner: the trigger is 44px with the
   18px icon centred, so the glyph corner sits 13px in from the box edge. */
.chat-more-dot {
  position: absolute;
  top: 9px;
  right: 9px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 2px var(--bg);
  pointer-events: none;
}
.chat-more-menu {
  z-index: 100;
  box-sizing: border-box;
  min-width: 220px;
  padding: var(--space-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 16px 36px -12px rgb(0 0 0 / 60%);
}
.chat-more-menu button {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  min-height: var(--touch);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-base);
  text-align: left;
}
.chat-more-menu button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.chat-more-menu button:hover,
.chat-more-menu button[data-highlighted] { background: var(--bg3); }
.chat-more-count {
  margin-left: auto;
  color: var(--fg3);
  font: 600 var(--text-xs) var(--font-mono);
  font-variant-numeric: tabular-nums;
}
.chat-more-sep {
  height: 1px;
  margin: var(--space-1) 0;
  background: var(--border);
}
</style>
