<script setup lang="ts">
// The handle that starts a drag-sort move (see composables/useDragSort.ts).
// The parent spreads `gripHandlers(id, scopeId)` onto it with `v-bind` and
// marks the row it lives in with `data-drag-row`, so the grip shows on that
// row's hover and focus-within on a fine pointer. It emits nothing, is not an
// HTML drag source, and never lets a click reach the row (the card or project
// toggle behind it).
defineProps<{
  /** Accessible name, for example "Reorder Ship the board". */
  label: string
  /** Space-separated key list for `aria-keyshortcuts`. */
  shortcutHint?: string
  /** The sortable item's id; the engine finds the grip by it. */
  id: string
  /** The grip is shown but cannot start a move; `title` then says why. */
  disabled?: boolean
  /** Replaces the label as the tooltip, for example the reason it is disabled. */
  title?: string
}>()
</script>

<template>
  <button
    type="button"
    class="drag-grip"
    :data-drag-handle="id"
    :aria-label="label"
    :title="title ?? label"
    :disabled="disabled"
    aria-roledescription="sortable handle"
    :aria-keyshortcuts="shortcutHint ?? 'Space Enter'"
    draggable="false"
    @click.stop
  >
    <svg
      class="drag-grip-glyph"
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="currentColor"
      aria-hidden="true"
    >
      <circle cx="4.5" cy="3" r="1.25" />
      <circle cx="9.5" cy="3" r="1.25" />
      <circle cx="4.5" cy="7" r="1.25" />
      <circle cx="9.5" cy="7" r="1.25" />
      <circle cx="4.5" cy="11" r="1.25" />
      <circle cx="9.5" cy="11" r="1.25" />
    </svg>
  </button>
</template>

<style scoped>
.drag-grip {
  position: relative;
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg2);
  cursor: grab;
  touch-action: none;
}

.drag-grip:disabled {
  cursor: not-allowed;
}

.drag-grip:active {
  cursor: grabbing;
}

.drag-grip:hover {
  background: var(--bg3);
  color: var(--fg);
}

.drag-grip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

@media (hover: hover) and (pointer: fine) {
  .drag-grip {
    opacity: 0;
  }

  .drag-grip:hover,
  .drag-grip:focus-visible,
  :global([data-drag-row]:hover) .drag-grip,
  :global([data-drag-row]:focus-within) .drag-grip {
    opacity: 1;
  }

  .drag-grip:disabled:hover,
  :global([data-drag-row]:hover) .drag-grip:disabled,
  :global([data-drag-row]:focus-within) .drag-grip:disabled {
    opacity: 0.4;
  }
}

/* A 44px target that does not widen the row: the glyph stays small. */
@media (pointer: coarse) {
  .drag-grip:disabled {
    opacity: 0.4;
  }

  .drag-grip::after {
    content: '';
    position: absolute;
    top: 50%;
    left: 50%;
    width: 44px;
    height: 44px;
    transform: translate(-50%, -50%);
  }
}
</style>
