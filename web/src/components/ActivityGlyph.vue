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
      <i class="activity-glyph-ring"></i>
      <i class="activity-glyph-orbit"></i>
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
// lit pixel chases the perimeter; a background run is a ring with one dot
// orbiting it. Anything that wants the owner (a question, a failure, a stuck
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

/* A background run: a ring with a satellite dot orbiting it. */
.activity-glyph--run {
  width: 10px;
  height: 10px;
}

.activity-glyph-ring {
  box-sizing: border-box;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 1.5px solid color-mix(in srgb, var(--accent) 55%, transparent);
}

.activity-glyph-orbit {
  position: absolute;
  top: -2.5px;
  left: calc(50% - 2px);
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--accent);
  transform-origin: 2px 7.5px;
  animation: activity-glyph-orbit 1.6s linear infinite;
}

/* The alert: a filled triangle with a knocked-out "!". */
.activity-glyph--alert { color: var(--accent); }
.activity-glyph-knock { fill: var(--on-accent); }

@keyframes activity-glyph-chase {
  0% { opacity: 1; }
  35% { opacity: 0.55; }
  70%, 100% { opacity: var(--pixel-rest); }
}

@keyframes activity-glyph-orbit {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .activity-glyph-pixel,
  .activity-glyph-orbit {
    animation: none;
  }

  /* Frozen into a shape, never a bare dot: the working grid a checker, the
     run its ring with the satellite at the top. */
  .activity-glyph-pixel--still { opacity: 1; }
}
</style>
