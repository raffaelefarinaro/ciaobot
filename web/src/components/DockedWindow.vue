<template>
  <section class="docked-window" :aria-labelledby="headingId">
    <div class="docked-window-head">
      <h2 :id="headingId" class="rail-title">{{ title }}</h2>
    </div>
    <slot />
  </section>
</template>

<script setup lang="ts">
/*
 * A page rail's docked window: Home's *At a glance* and the Automations
 * *Recent runs*. The Chat details window's vocabulary without its close (a
 * rail window is part of the page, not a dismissible panel): the tonal --bg2
 * surface, a 1px border, the 14px radius, the docked shadow, and a 44px title
 * bar with 16px insets over hairline rows.
 *
 * API, kept small so any rail can adopt it unchanged:
 * - prop `title` (required): the title bar's text and the window's accessible
 *   name (a `<section>` labelled by its `<h2 class="rail-title">`);
 * - prop `id` (optional): the heading's id, for a caller or test that needs a
 *   stable one; otherwise one is generated;
 * - default slot: the window's body, anything at all;
 * - optional row classes for the body: `docked-row` (a link or button row)
 *   holding `docked-row-title` (two lines at most) over `docked-row-detail`;
 *   `docked-row--static` for a row that opens nothing; a `docked-list--end`
 *   wrapper drops the last row's rule where the list ends the window.
 * Host classes land on the root.
 */
import { useId } from 'vue'

const props = defineProps<{
  /** The window's title, its accessible name. */
  title: string
  /** The heading's id, when a caller or test needs a stable one. */
  id?: string
}>()

const headingId = props.id || `docked-${useId()}`
</script>

<style scoped>
.docked-window {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg2);
  box-shadow: 0 12px 32px -14px rgb(0 0 0 / 45%);
  font-size: var(--text-sm);
}

:global(:root.theme-light .docked-window) {
  box-shadow: 0 10px 28px -14px rgb(26 26 46 / 28%);
}

.docked-window-head {
  display: flex;
  align-items: center;
  min-height: 44px;
  margin: -16px -16px 6px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
}

.docked-window-head .rail-title {
  margin: 0;
}

</style>

<!-- The rows are the host's own elements, often inside a RouterLink, where a
     scoped :slotted() rule does not reach; these rules are scoped by the
     window's class instead. -->
<style>
/* Hairline rows, not cards: a short list, not a second inbox to clear. */
.docked-window .docked-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  box-sizing: border-box;
  width: 100%;
  min-height: var(--touch);
  padding: 13px 0;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  text-decoration: none;
  cursor: pointer;
}

/* A list that ends the window drops the last rule: the window's edge closes it. */
.docked-window .docked-list--end > .docked-row:last-child {
  border-bottom: 0;
}

.docked-window .docked-row--static {
  cursor: default;
}

.docked-window .docked-row:not(.docked-row--static):hover .docked-row-title {
  color: var(--accent);
}

.docked-window .docked-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}

/* Two lines at most: a long automation title wraps once, then ellipsises. */
.docked-window .docked-row-title {
  display: -webkit-box;
  min-width: 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  font-size: calc(13px * var(--font-scale));
  font-weight: 650;
  line-height: 1.35;
  overflow-wrap: anywhere;
  transition: color 120ms var(--ease);
}

.docked-window .docked-row-detail {
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.4;
  overflow-wrap: anywhere;
}

@media (prefers-reduced-motion: reduce) {
  .docked-window .docked-row-title {
    transition: none;
  }
}
</style>
