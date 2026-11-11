<template>
  <RouterLink v-if="to" :to="to" class="sidebar-row" :class="{ active }" active-class="active">
    <span v-if="pending" class="title-shimmer" aria-hidden="true" title="Naming conversation" />
    <span v-else class="sidebar-row-title" :class="titleClass">{{ text }}</span>
    <span v-if="pending" class="sr-only">Naming conversation</span>
    <span v-if="meta" class="sidebar-row-meta">{{ meta }}</span>
    <slot />
  </RouterLink>
  <div v-else class="sidebar-row" :class="{ active }">
    <!-- A bare span is role=generic, which cannot be named: the bar is hidden
         from assistive tech and the name travels as text beside it. The row is
         not a live region; only the open item's header announces the naming. -->
    <span v-if="pending" class="title-shimmer" aria-hidden="true" title="Naming conversation" />
    <span v-else class="sidebar-row-title" :class="titleClass">{{ text }}</span>
    <span v-if="pending" class="sr-only">Naming conversation</span>
    <span v-if="meta" class="sidebar-row-meta">{{ meta }}</span>
    <slot />
  </div>
</template>

<script setup lang="ts">
/*
 * One sidebar list row: a chat under its project, an automation under its group.
 * Title on the left, then an optional muted mono badge (an automation's time or
 * cadence) and whatever marks the default slot holds (activity glyphs, row
 * actions). The host adds its own classes, handlers and attributes; they land
 * on the root.
 */
import { computed } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'

const props = defineProps<{
  /** The row's title. */
  text: string
  /** A link row; without it the row is a plain element the host wires up. */
  to?: RouteLocationRaw
  active?: boolean
  /** Full-strength title: the item has something new. */
  unread?: boolean
  /** Quiet title: the item is paused or otherwise inert. */
  muted?: boolean
  /** The title is still being named: the shimmer stands in for it. */
  pending?: boolean
  /** Muted mono badge after the title (time, cadence). */
  meta?: string
}>()

// `chat-title` is kept for the sidebar's chat-row tests and selectors.
const titleClass = computed(() => ({
  'chat-title': true,
  'sidebar-row-title--unread': props.unread,
  'chat-title--unread': props.unread,
  'sidebar-row-title--muted': props.muted && !props.unread,
}))
</script>

<style scoped src="./titleShimmer.css"></style>

<style scoped>
/* The sidebar's one row language: title left, marks right, a 7px-rounded fill on
   hover and when selected (fill and the brighter label, no accent bar). 44px on
   touch, 36px with a fine pointer. */
.sidebar-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  min-height: var(--touch);
  box-sizing: border-box;
  padding: 0 4px 0 10px;
  border-radius: 7px;
  color: var(--fg2);
  font-size: var(--text-base);
  text-align: left;
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  cursor: pointer;
}
@media (pointer: fine) {
  .sidebar-row { min-height: 36px; }
}
.sidebar-row:hover {
  background: var(--bg-elev);
  color: var(--fg);
}
.sidebar-row.active {
  background: var(--bg3);
  color: var(--fg);
}
.sidebar-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.sidebar-row-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Unread is full-strength text plus the row's dot, not bold: bold titles read
   as a second level of headings next to the projects. */
.sidebar-row-title--unread {
  color: var(--fg);
  font-weight: 500;
}
.sidebar-row-title--muted { color: var(--fg3); }
.sidebar-row-meta {
  flex: none;
  padding-right: 6px;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
</style>
