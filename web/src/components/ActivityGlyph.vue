<template>
  <span class="activity-glyph" :class="`activity-glyph--${kind}`" aria-hidden="true"></span>
</template>

<script setup lang="ts">
// One mark per kind of live work, so a working turn, a subagent and a shell
// run no longer draw the same pulsing dot. Shape carries the meaning, not
// colour: working pulses, an agent orbits, a run is a diamond with a dot tracing
// its edges. Decorative only: the caller owns the accessible name.
export type ActivityGlyphKind = 'working' | 'agent' | 'run' | 'run-failed'
defineProps<{ kind: ActivityGlyphKind }>()
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

/* The chat's own turn: a solid accent dot with a halo and an expanding ring. */
.activity-glyph--working {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 4px var(--accent);
  animation: activity-glyph-pulse 1.1s ease-in-out infinite;
}

.activity-glyph--working::before {
  content: "";
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  background: var(--accent);
  opacity: 0.45;
  animation: activity-glyph-ring 1.1s ease-out infinite;
  pointer-events: none;
}

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

/* A shell run: a diamond whose edges a dot traces, neutral. Same motion
   language as the orbit, a shape neither the agent ring nor the task
   checkbox uses. Failure is red and still. */
.activity-glyph--run,
.activity-glyph--run-failed {
  box-sizing: border-box;
  width: 9px;
  height: 9px;
  margin: 2px; /* the rotated diagonal is ~12.7px */
  transform: rotate(45deg);
  color: var(--fg2);
  border: 1.5px solid color-mix(in srgb, currentColor 55%, transparent);
}

.activity-glyph--run::after,
.activity-glyph--run-failed::after {
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

.activity-glyph--run-failed { color: var(--error); }
.activity-glyph--run-failed::after { animation: none; }

@keyframes activity-glyph-trace {
  from { offset-distance: 0%; }
  to { offset-distance: 100%; }
}

@keyframes activity-glyph-pulse {
  0%, 100% { transform: scale(0.55); opacity: 0.35; }
  50% { transform: scale(1); opacity: 1; }
}

@keyframes activity-glyph-ring {
  0% { transform: scale(0.6); opacity: 0.45; }
  100% { transform: scale(1.6); opacity: 0; }
}

@keyframes activity-glyph-orbit {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .activity-glyph--working,
  .activity-glyph--working::before,
  .activity-glyph--agent::after,
  .activity-glyph--run::after {
    animation: none;
  }

  .activity-glyph--working { opacity: 1; }
  .activity-glyph--working::before { opacity: 0.3; }
}
</style>
