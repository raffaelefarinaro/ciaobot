<template>
  <RouterLink v-if="to" :to="to" class="list-row">
    <span class="list-row-main">
      <span class="list-row-heading"><slot name="heading"><span class="list-row-title">{{ text }}</span></slot></span>
      <span v-if="$slots.sub" class="list-row-sub"><slot name="sub" /></span>
      <slot name="detail" />
    </span>
    <span v-if="$slots.end" class="list-row-end"><slot name="end" /></span>
  </RouterLink>
  <button v-else type="button" class="list-row">
    <span class="list-row-main">
      <span class="list-row-heading"><slot name="heading"><span class="list-row-title">{{ text }}</span></slot></span>
      <span v-if="$slots.sub" class="list-row-sub"><slot name="sub" /></span>
      <slot name="detail" />
    </span>
    <span v-if="$slots.end" class="list-row-end"><slot name="end" /></span>
  </button>
</template>

<script setup lang="ts">
/*
 * One row of a page-level list: Home's chats and the Automations overview.
 * A flat hairline row, the title over one muted sub-line on the left and a
 * right-hand end column (a time, a state, the chat's signals). A link when it
 * has `to`, otherwise a button the host wires up; the host's classes, handlers
 * and attributes land on the root.
 *
 * Slots: `heading` replaces the plain title (a naming shimmer, marks after the
 * title); give its title text the `list-row-title` class to share the title
 * style. `sub` is the sub-line, `detail` any clamped lines under it (a pending
 * question, a reply snippet), `end` the right column; give its time or state
 * text the `list-row-when` class for the muted mono. Keyboard roaming over
 * these rows is `roamFocus` in lib/listRoaming.ts.
 */
import { RouterLink, type RouteLocationRaw } from 'vue-router'

defineProps<{
  /** A link row; without it the row is a button. */
  to?: RouteLocationRaw
  /** The plain title, when the `heading` slot is not used. (Not `title`: a
   *  host's `title` stays the root's tooltip.) */
  text?: string
}>()
</script>

<style scoped>
.list-row {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  width: 100%;
  min-width: 0;
  min-height: 56px;
  box-sizing: border-box;
  padding: 9px 2px;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  text-align: left;
  text-decoration: none;
  transition: background 120ms var(--ease);
}
.list-row:active { background: color-mix(in srgb, var(--accent) 5%, transparent); }
.list-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
  box-shadow: 0 0 0 2px var(--bg);
}
.list-row:disabled { cursor: default; }
.list-row-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.list-row-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
/* The default title, and a host's own title span in the heading slot. */
.list-row-title,
:slotted(.list-row-title) {
  display: block;
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: color 120ms var(--ease);
}
.list-row:hover .list-row-title,
.list-row:hover :slotted(.list-row-title) { color: var(--accent); }
.list-row-sub {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.list-row-end {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  align-items: flex-end;
  justify-content: center;
  gap: 4px;
  min-width: 5ch;
  text-align: right;
  white-space: nowrap;
}
/* The end column's time or state text. The column itself sets no type, so
   what else sits there (Home's ChatSignals) keeps its own. */
:slotted(.list-row-when) {
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}
@media (prefers-reduced-motion: reduce) {
  .list-row,
  .list-row-title,
  :slotted(.list-row-title) { transition: none; }
}
</style>
