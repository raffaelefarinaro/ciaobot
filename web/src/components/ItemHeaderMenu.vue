<template>
  <DropdownMenuRoot :modal="false">
    <DropdownMenuTrigger as-child>
      <button
        type="button"
        class="btn-icon item-menu-trigger"
        :class="{ 'item-menu-trigger--phone': phoneOnly }"
        :aria-label="label"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" />
        </svg>
      </button>
    </DropdownMenuTrigger>
    <DropdownMenuPortal>
      <DropdownMenuContent as-child align="end" :side-offset="6" :collision-padding="8">
        <!-- as-child, so the panel is this component's own element and keeps
             its scoped styles although it is portalled. -->
        <div class="item-header-menu"><slot /></div>
      </DropdownMenuContent>
    </DropdownMenuPortal>
  </DropdownMenuRoot>
</template>

<script setup lang="ts">
/*
 * The ⋯ menu of an open item's header (an automation, a task). The host puts
 * `DropdownMenuItem as-child` buttons in the slot; a button that mirrors a
 * header action only on a phone carries `fold-phone` (headerFold.css), the
 * header action itself `fold-wide`. With `phoneOnly` the trigger shows only
 * on a phone, for an item whose wide header holds everything inline.
 */
import { DropdownMenuContent, DropdownMenuPortal, DropdownMenuRoot, DropdownMenuTrigger } from 'reka-ui'

defineProps<{
  /** The trigger's accessible name ("More task actions"). */
  label: string
  phoneOnly?: boolean
}>()
</script>

<style scoped>
.item-menu-trigger { color: var(--fg2); }
@media (min-width: 768px) {
  .item-menu-trigger--phone { display: none; }
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
.item-header-menu :slotted(button) {
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
.item-header-menu :slotted(button[data-highlighted]) { background: var(--bg3); outline: none; }
.item-header-menu :slotted(button:disabled) { color: var(--fg3); cursor: default; }
.item-header-menu :slotted(button.danger) { color: var(--error); }
/* A note above the items (the task's save line): quiet, not an action. */
.item-header-menu :slotted(.item-header-menu-note) {
  margin: 0;
  padding: 6px 12px;
  color: var(--fg3);
  font-size: var(--text-xs);
}
</style>
