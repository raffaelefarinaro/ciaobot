<template>
  <span class="activity-glyph" :class="`activity-glyph--${kind}`" aria-hidden="true">
    <template v-if="kind === 'working'">
      <i
        v-for="(order, cell) in WORKING_CHASE"
        :key="cell"
        class="activity-glyph-pixel"
        :class="{
          'activity-glyph-pixel--centre': order < 0,
          'activity-glyph-pixel--still': cell % 2 === 0,
        }"
        :style="{ '--i': order }"
      ></i>
    </template>
    <svg
      v-else-if="kind === 'run-failed'"
      width="12"
      height="11"
      viewBox="0 0 16 14"
      aria-hidden="true"
    >
      <path d="M8 1.2L15 12.8H1Z" fill="currentColor" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
      <rect x="7.2" y="4.6" width="1.6" height="4.4" class="activity-glyph-knock" />
      <rect x="7.2" y="10" width="1.6" height="1.6" class="activity-glyph-knock" />
    </svg>
  </span>
</template>

<script setup lang="ts">
// One mark per kind of live work, all in the row's accent: shape and motion
// carry the meaning, never hue, because an accent preset can sit next to any
// semantic colour. Running work moves: a working turn is a 3x3 pixel grid whose
// lit pixel chases the perimeter, a subagent orbits, a run is a diamond with a
// dot tracing its edges. Something broke is a filled alert triangle, still.
// Decorative only: the caller owns the accessible name.
export type ActivityGlyphKind = 'working' | 'agent' | 'run' | 'run-failed'
defineProps<{ kind: ActivityGlyphKind }>()

// Chase order of the 9 grid cells in row-major order; -1 is the centre, which
// never animates.
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

/* The chat's own turn: a 3x3 pixel grid, one lit pixel chasing the perimeter. */
.activity-glyph--working {
  display: inline-grid;
  grid-template-columns: repeat(3, 3px);
  gap: 1px;
}

.activity-glyph-pixel {
  width: 3px;
  height: 3px;
  background: var(--accent);
  opacity: 0.18;
  animation: activity-glyph-chase 1.2s linear infinite;
  animation-delay: calc(var(--i) * 0.15s - 1.2s);
}

.activity-glyph-pixel--centre { animation: none; }

/* A subagent: a ring with a satellite dot orbiting it. */
.activity-glyph--agent {
  box-sizing: border-box;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 1.5px solid color-mix(in srgb, var(--accent) 55%, transparent);
}

.activity-glyph--agent::after {
  content: "";
  position: absolute;
  top: -2.5px;
  left: calc(50% - 2px);
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--accent);
  transform-origin: 2px 6.5px;
  animation: activity-glyph-orbit 1.6s linear infinite;
}

/* A shell run: a diamond whose edges a dot traces. Same motion language as
   the orbit, a shape neither the agent ring nor the task checkbox uses. */
.activity-glyph--run {
  box-sizing: border-box;
  width: 9px;
  height: 9px;
  margin: 2px; /* the rotated diagonal is ~12.7px */
  transform: rotate(45deg);
  color: var(--accent);
  border: 1.5px solid color-mix(in srgb, currentColor 55%, transparent);
}

.activity-glyph--run::after {
  content: "";
  position: absolute;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
  offset-path: path('M-0.75,-0.75 H6.75 V6.75 H-0.75 Z');
  offset-rotate: 0deg;
  animation: activity-glyph-trace 1.8s linear infinite;
}

/* Failure is a filled alert triangle with a knocked-out "!". */
.activity-glyph--run-failed { color: var(--accent); }
.activity-glyph-knock { fill: var(--on-accent); }

@keyframes activity-glyph-trace {
  from { offset-distance: 0%; }
  to { offset-distance: 100%; }
}

@keyframes activity-glyph-chase {
  0% { opacity: 1; }
  35% { opacity: 0.55; }
  70%, 100% { opacity: 0.18; }
}

@keyframes activity-glyph-orbit {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .activity-glyph--agent::after,
  .activity-glyph--run::after,
  .activity-glyph-pixel {
    animation: none;
  }

  /* Frozen into a checker (corners and centre), never a bare dot. */
  .activity-glyph-pixel--still { opacity: 1; }
}
</style>
