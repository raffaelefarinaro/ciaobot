<script setup lang="ts">
/**
 * The sidebar's first-load placeholder: groups of `SidebarRow` placeholder rows
 * under a heading bar, so the chat list, the automation list and the Memory
 * Map's filters all load in the shape of the rows they are about to show.
 * One status for assistive tech, named by `label`; the shapes are hidden.
 *
 * `nested` hangs the rows under their heading the way chats hang under their
 * project (indented, with the list's rule on the left).
 */
import SidebarRow from './SidebarRow.vue'

withDefaults(
  defineProps<{
    /** What is loading, as a screen reader should hear it. */
    label: string
    groups?: number
    /** Rows per group. */
    rows?: number
    nested?: boolean
  }>(),
  { groups: 1, rows: 3, nested: false },
)

/** Fixed, varied widths so the rows read as titles, not as bars. */
const WIDTHS = ['68%', '54%', '76%', '60%', '72%']
</script>

<template>
  <div class="sidebar-skeleton" role="status" aria-live="polite" :aria-label="label">
    <div v-for="g in groups" :key="g" class="sidebar-skeleton-group" aria-hidden="true">
      <div class="sidebar-skeleton-heading"><span class="sidebar-skeleton-heading-bar" /></div>
      <div class="sidebar-skeleton-rows" :class="{ 'sidebar-skeleton-rows--nested': nested }">
        <SidebarRow
          v-for="r in rows"
          :key="r"
          text=""
          :placeholder="WIDTHS[(g + r) % WIDTHS.length]"
        />
      </div>
    </div>
    <span class="sr-only">{{ label }}…</span>
  </div>
</template>

<style scoped>
.sidebar-skeleton {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}
.sidebar-skeleton-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
/* A group label's line: the size of the Routines or project heading's text. */
.sidebar-skeleton-heading {
  display: flex;
  align-items: center;
  min-height: 28px;
  padding: 0 10px;
}
.sidebar-skeleton-heading-bar {
  display: block;
  width: 40%;
  height: 10px;
  border-radius: 999px;
  background: var(--bg3);
  opacity: 0.7;
}
.sidebar-skeleton-rows {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
/* Chats sit under their project: the chat list's indent and rule. */
.sidebar-skeleton-rows--nested {
  margin: 2px 0 2px 21px;
  border-left: 1px solid var(--border);
}
</style>
