<template>
  <span class="activity-glyph" :class="`activity-glyph--${kind}`" aria-hidden="true">
    <svg
      v-if="kind === 'run-failed'"
      width="12"
      height="11"
      viewBox="0 0 16 14"
      aria-hidden="true"
    >
      <path d="M8 1.2L15 12.8H1Z" fill="currentColor" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
      <rect x="7.2" y="4.6" width="1.6" height="4.4" class="activity-glyph-knock" />
      <rect x="7.2" y="10" width="1.6" height="1.6" class="activity-glyph-knock" />
    </svg>
    <template v-else>
      <i
        v-for="(order, cell) in PIXEL_GRIDS[kind].chase"
        :key="cell"
        class="activity-glyph-pixel"
        :class="{
          'activity-glyph-pixel--fixed': order < 0,
          'activity-glyph-pixel--still': PIXEL_GRIDS[kind].still.includes(cell),
        }"
        :style="{ '--i': order }"
      ></i>
    </template>
  </span>
</template>

<script setup lang="ts">
// One mark per kind of live work, all in the row's accent: shape and motion
// carry the meaning, never hue, because an accent preset can sit next to any
// semantic colour. Running work is a 3x3 pixel grid whose lit pixel chases the
// perimeter: a working turn (or its subagents) over a dim grid, a background
// run as a lit hub with one pixel orbiting it and nothing else drawn.
// Something broke is a filled alert triangle, still.
// Decorative only: the caller owns the accessible name.
export type ActivityGlyphKind = 'working' | 'run' | 'run-failed'
defineProps<{ kind: ActivityGlyphKind }>()

// Per kind, the chase step of each of the 9 cells in row-major order (-1 never
// animates: the dim centre of `working`, the lit hub of `run`), and the cells
// lit in the frozen reduced-motion frame.
const PIXEL_GRIDS = {
  working: { chase: [0, 1, 2, 7, -1, 3, 6, 5, 4], still: [0, 2, 4, 6, 8] },
  run: { chase: [0, 1, 2, 7, -1, 3, 6, 5, 4], still: [2, 4] },
} satisfies Record<Exclude<ActivityGlyphKind, 'run-failed'>, { chase: number[]; still: number[] }>
</script>

<style scoped>
.activity-glyph {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  line-height: 1;
}

/* Running work: a 3x3 pixel grid, one lit pixel chasing a path through it
   and leaving a short tail. The kinds differ in rest level and hub. */
.activity-glyph--working,
.activity-glyph--run {
  --pixel-rest: 0.18;
  display: inline-grid;
  grid-template-columns: repeat(3, 3px);
  gap: 1px;
}

/* A background run: no grid, just a lit hub and the pixel orbiting it. */
.activity-glyph--run { --pixel-rest: 0; }

.activity-glyph-pixel {
  width: 3px;
  height: 3px;
  background: var(--accent);
  opacity: var(--pixel-rest);
  animation: activity-glyph-chase 1.2s linear infinite;
  animation-delay: calc(var(--i) * 0.15s - 1.2s);
}

.activity-glyph-pixel--fixed { animation: none; }
.activity-glyph--run .activity-glyph-pixel--fixed { opacity: 1; }

/* Failure is a filled alert triangle with a knocked-out "!". */
.activity-glyph--run-failed { color: var(--accent); }
.activity-glyph-knock { fill: var(--on-accent); }

@keyframes activity-glyph-chase {
  0% { opacity: 1; }
  35% { opacity: 0.55; }
  70%, 100% { opacity: var(--pixel-rest); }
}

@media (prefers-reduced-motion: reduce) {
  .activity-glyph-pixel {
    animation: none;
  }

  /* Frozen into a shape per kind, never a bare dot: the working grid a
     checker, the run its hub and satellite. */
  .activity-glyph-pixel--still { opacity: 1; }
}
</style>
