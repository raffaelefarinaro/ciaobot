<template>
  <div class="item-title-row" :class="{ 'item-title-row--rail': rail }">
    <!-- The 18px stroke × in the header's shared .btn-icon box, so it reads as
         one of the header's icons and shares the title's vertical centre. -->
    <button
      type="button"
      class="btn-icon close-btn"
      :title="closeTitle ?? closeLabel"
      :aria-label="closeLabel"
      @click="emit('close')"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <line x1="18" y1="6" x2="6" y2="18"/>
        <line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
    </button>
    <slot />
  </div>
</template>

<script setup lang="ts">
/*
 * The title row of an open item's header: a chat, an automation, a project, a
 * task. Close, then whatever the slot holds: the TitleRail over the item's
 * neighbours, or the plain title (or a rename field). With `rail` the row takes
 * the header's room, so the rail can centre the title on the item's reading
 * column. On a phone (767px and under) close is hidden so the title keeps the
 * room: the menu and Back already leave the item.
 *
 * The header's actions fold the same way on every item: `headerFold.css`'s
 * `fold-wide` / `fold-phone` classes, and `ItemHeaderMenu` for the ⋯ menu.
 */
withDefaults(defineProps<{
  /** The close button's accessible name ("Close chat", "Close task"). */
  closeLabel: string
  /** Its tooltip, when it differs from the name. */
  closeTitle?: string
  /** The title is a rail: the row takes the header's width. */
  rail?: boolean
}>(), { rail: false })

const emit = defineEmits<{ close: [] }>()
</script>

<style scoped>
.item-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
}
.item-title-row--rail { flex: 1 1 auto; width: 100%; }
/* Sizing, hover fill and touch padding come from PaneHeader's shared .btn-icon
   rules; this sets the quiet resting colour of the trailing icons. */
.close-btn { color: var(--fg3); flex: none; }
.close-btn:hover { color: var(--fg); }
@media (max-width: 767px) {
  .close-btn { display: none; }
}
</style>
