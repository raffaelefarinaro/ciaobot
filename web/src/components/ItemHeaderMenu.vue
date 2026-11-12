<template>
  <span class="item-menu">
    <DropdownMenuRoot :modal="false">
      <DropdownMenuTrigger as-child>
        <button
          ref="triggerEl"
          type="button"
          class="btn-icon item-menu-trigger"
          :class="{ 'item-menu-trigger--phone': phoneOnly }"
          :aria-label="triggerLabel"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" />
          </svg>
          <span v-if="inbox.count > 0" class="item-menu-dot" aria-hidden="true"></span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuPortal>
        <!-- as-child, so the panel is this component's own element and keeps
             its scoped styles although it is portalled. -->
        <DropdownMenuContent
          as-child
          align="end"
          :side-offset="6"
          :collision-padding="8"
          @close-auto-focus="onCloseAutoFocus"
        >
          <div class="item-header-menu">
            <slot />
            <!-- On a phone the header's Back and bell fold in here, as in a chat's
                 ⋯ menu; a wide pane shows them in the header, so these hide there. -->
            <DropdownMenuSeparator class="item-menu-sep item-menu-nav" />
            <DropdownMenuItem as-child class="item-menu-nav" @select="requestSheet">
              <button type="button">
                <span>Notifications</span>
                <span v-if="inbox.count > 0" class="item-menu-count" aria-hidden="true">{{ inbox.badgeText }}</span>
              </button>
            </DropdownMenuItem>
            <DropdownMenuItem v-if="historyNav.canBack.value" as-child class="item-menu-nav" @select="historyNav.back">
              <button type="button">{{ historyNav.backLabel.value }}</button>
            </DropdownMenuItem>
          </div>
        </DropdownMenuContent>
      </DropdownMenuPortal>
    </DropdownMenuRoot>
    <!-- The sheet has no button of its own here: the ⋯ trigger is its anchor. -->
    <NotificationBell ref="bell" :trigger="false" variant="sheet" />
  </span>
</template>

<script setup lang="ts">
/*
 * The ⋯ menu of an open item's header (an automation, a task). The host puts
 * `DropdownMenuItem as-child` buttons in the slot; a button that mirrors a
 * header action only on a phone carries `fold-phone` (headerFold.css), the
 * header action itself `fold-wide`. With `phoneOnly` the trigger shows only
 * on a phone, for an item whose wide header holds everything inline. On a
 * phone the menu also carries Notifications and, with in-app history, Back,
 * the header's own copies of which the host hides with `phone-overflow`.
 */
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

const props = defineProps<{
  /** The trigger's accessible name ("More task actions"). */
  label: string
  phoneOnly?: boolean
}>()

const inbox = useNotificationInboxStore()
const historyNav = useHistoryNav()

const triggerEl = ref<HTMLButtonElement | null>(null)
const bell = ref<InstanceType<typeof NotificationBell> | null>(null)
let sheetRequested = false

const triggerLabel = computed(() => (
  inbox.count > 0 ? `${props.label}, ${inbox.count} unread notifications` : props.label
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
</script>

<style scoped>
.item-menu { display: contents; }
.item-menu-trigger {
  position: relative;
  color: var(--fg2);
}
/* The dot hangs off the glyph's top-right corner: the trigger is 44px with the
   18px icon centred, so the glyph corner sits 13px in from the box edge. */
.item-menu-dot {
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
@media (min-width: 768px) {
  .item-menu-trigger--phone { display: none; }
  .item-menu-dot { display: none; }
  .item-menu-nav { display: none !important; }
}
.item-header-menu {
  z-index: 100;
  min-width: 200px;
  padding: 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 12px 32px rgb(0 0 0 / 35%);
}
.item-header-menu :slotted(button),
.item-header-menu > button {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: var(--touch);
  padding: 8px 12px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.item-header-menu :slotted(button:hover),
.item-header-menu :slotted(button[data-highlighted]),
.item-header-menu > button:hover,
.item-header-menu > button[data-highlighted] { background: var(--bg3); outline: none; }
.item-header-menu :slotted(button:disabled) { color: var(--fg3); cursor: default; }
.item-header-menu :slotted(button.danger) { color: var(--error); }
/* A note above the items (the task's save line): quiet, not an action. */
.item-header-menu :slotted(.item-header-menu-note) {
  margin: 0;
  padding: 6px 12px;
  color: var(--fg3);
  font-size: var(--text-xs);
}
.item-menu-count {
  margin-left: auto;
  color: var(--fg3);
  font: 600 var(--text-xs) var(--font-mono);
  font-variant-numeric: tabular-nums;
}
.item-menu-sep {
  height: 1px;
  margin: 4px 0;
  background: var(--border);
}
</style>
