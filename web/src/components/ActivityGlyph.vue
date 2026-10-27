<template>
  <span class="activity-glyph" :class="`activity-glyph--${kind}`" aria-hidden="true">
    <svg
      v-if="kind === 'alert'"
      width="12"
      height="11"
      viewBox="0 0 16 14"
      aria-hidden="true"
    >
      <path d="M8 1.2L15 12.8H1Z" fill="currentColor" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
      <rect x="7.2" y="4.6" width="1.6" height="4.4" class="activity-glyph-knock" />
      <rect x="7.2" y="10" width="1.6" height="1.6" class="activity-glyph-knock" />
    </svg>
    <template v-else-if="kind === 'run'">
      <svg class="activity-glyph-prompt" width="7" height="11" viewBox="0 0 7 11" aria-hidden="true">
        <path d="M3.5 0.5V10.5M6 3.1C5.8 2.2 4.9 1.6 3.5 1.6 2 1.6 1 2.3 1 3.4 1 4.6 2.2 5 3.5 5.4 4.8 5.8 6 6.3 6 7.6 6 8.7 5 9.4 3.5 9.4 2 9.4 1 8.8 0.9 7.9" />
      </svg>
      <i class="activity-glyph-cursor"></i>
    </template>
    <template v-else>
      <i
        v-for="(order, cell) in WORKING_CHASE"
        :key="cell"
        class="activity-glyph-pixel"
        :class="{
          'activity-glyph-pixel--fixed': order < 0,
          'activity-glyph-pixel--still': cell % 2 === 0,
        }"
        :style="{ '--i': order }"
      ></i>
    </template>
  </span>
</template>

<script setup lang="ts">
// One mark per kind of live work, all in the row's accent: shape and motion
// carry the meaning, never hue, because an accent preset can sit next to any
// semantic colour. A working turn (or its subagents) is a 3x3 pixel grid whose
// lit pixel chases the perimeter; a background run is a shell prompt, `$` and
// a blinking block cursor. Anything that wants the owner (a question, a failure, a stuck
// pass) is a filled alert triangle, still.
// Decorative only: the caller owns the accessible name.
export type ActivityGlyphKind = 'working' | 'run' | 'alert'
defineProps<{ kind: ActivityGlyphKind }>()

// Chase step of each of the 9 grid cells in row-major order; -1 is the dim
// centre, which never animates. The even cells are the reduced-motion checker.
const WORKING_CHASE = [0, 1, 2, 7, -1, 3, 6, 5, 4]
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

/* A working turn: a 3x3 pixel grid, one lit pixel chasing the perimeter and
   leaving a short tail over a dim grid. */
.activity-glyph--working {
  --pixel-rest: 0.18;
  display: inline-grid;
  grid-template-columns: repeat(3, 3px);
  gap: 1px;
}

.activity-glyph-pixel {
  width: 3px;
  height: 3px;
  background: var(--accent);
  opacity: var(--pixel-rest);
  animation: activity-glyph-chase 1.2s linear infinite;
  animation-delay: calc(var(--i) * 0.15s - 1.2s);
}

.activity-glyph-pixel--fixed { animation: none; }

/* A background run: a shell prompt waiting on its command, `$` and a block
   cursor that blinks. Sized to sit on the same 11px line as the grid. */
.activity-glyph--run {
  gap: 1px;
  height: 11px;
  color: var(--accent);
}

/* Drawn, not typed: a font's `$` sits on the baseline, below the cursor. */
.activity-glyph-prompt {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
}

.activity-glyph-cursor {
  width: 4px;
  height: 8px;
  background: currentColor;
  animation: activity-glyph-blink 1.1s steps(1, end) infinite;
}

/* The alert: a filled triangle with a knocked-out "!". */
.activity-glyph--alert { color: var(--accent); }
.activity-glyph-knock { fill: var(--on-accent); }

@keyframes activity-glyph-chase {
  0% { opacity: 1; }
  35% { opacity: 0.55; }
  70%, 100% { opacity: var(--pixel-rest); }
}

@keyframes activity-glyph-blink {
  50% { opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .activity-glyph-pixel,
  .activity-glyph-cursor {
    animation: none;
  }

  /* Frozen into a shape, never a bare dot: the working grid a checker, the
     run its prompt with a solid cursor. */
  .activity-glyph-pixel--still { opacity: 1; }
}
</style>
