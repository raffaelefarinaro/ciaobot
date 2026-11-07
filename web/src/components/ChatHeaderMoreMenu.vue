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
              <button type="button">
                <svg class="chat-more-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><line x1="12" y1="11" x2="12" y2="16" /><line x1="12" y1="8" x2="12.01" y2="8" /></svg>
                <span>Chat details</span>
              </button>
            </DropdownMenuItem>
            <DropdownMenuItem as-child @select="requestSheet">
              <button type="button">
                <svg class="chat-more-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true"><path d="M6 17V10a6 6 0 0 1 12 0v7" /><line x1="4" y1="17" x2="20" y2="17" /><line x1="10" y1="21" x2="14" y2="21" /></svg>
                <span>Notifications</span>
                <span v-if="inbox.count > 0" class="chat-more-count" aria-hidden="true">{{ inbox.badgeText }}</span>
              </button>
            </DropdownMenuItem>
            <DropdownMenuItem v-if="canArchive" as-child @select="emit('archive')">
              <button type="button">
                <svg class="chat-more-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="21 8 21 21 3 21 3 8" /><rect x="1" y="3" width="22" height="5" /><line x1="10" y1="12" x2="14" y2="12" /></svg>
                <span>Archive</span>
              </button>
            </DropdownMenuItem>
            <template v-if="historyNav.canBack.value">
              <DropdownMenuSeparator class="chat-more-sep" />
              <DropdownMenuItem as-child @select="historyNav.back">
                <button type="button">
                  <svg class="chat-more-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6" /></svg>
                  <span>{{ historyNav.backLabel.value }}</span>
                </button>
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
.chat-more-icon {
  flex-shrink: 0;
  color: var(--fg2);
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
