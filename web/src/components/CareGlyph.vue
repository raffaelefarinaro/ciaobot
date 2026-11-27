<template>
  <!-- Status as a shape: a tick, an exclamation in a ring, a dashed ring, or a
       turning arc. Decorative: every caller says the state in words beside it. -->
  <svg
    class="care-glyph"
    :class="`care-glyph--${kind}`"
    :width="size"
    :height="size"
    viewBox="0 0 20 20"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <template v-if="kind === 'done'">
      <circle cx="10" cy="10" r="8" />
      <path d="M6.4 10.3l2.5 2.5 4.8-5.2" />
    </template>
    <template v-else-if="kind === 'blocked'">
      <circle cx="10" cy="10" r="8" />
      <path d="M10 5.8v4.9M10 13.9v.1" />
    </template>
    <circle v-else-if="kind === 'open'" cx="10" cy="10" r="8" stroke-dasharray="2.6 2.6" />
    <template v-else>
      <circle class="care-glyph-track" cx="10" cy="10" r="8" />
      <path class="care-glyph-arc" d="M10 2a8 8 0 0 1 8 8" />
    </template>
  </svg>
</template>

<script setup lang="ts">
/*
 * The Workspace care status glyph (Home's At a glance line, the report page's
 * steps). Shape carries the state, never colour alone: done, blocked, open
 * (pending or never run) and running, whose arc turns unless reduced motion is
 * asked for.
 */
import type { CareGlyphKind } from '../lib/careReport'

withDefaults(defineProps<{ kind: CareGlyphKind; size?: number }>(), { size: 16 })
</script>

<style scoped>
.care-glyph { flex: 0 0 auto; display: block; }
.care-glyph--done { color: var(--success); }
.care-glyph--blocked { color: var(--warning); }
.care-glyph--open { color: var(--fg3); }
.care-glyph--running { color: var(--accent); }
.care-glyph-track { opacity: 0.25; }

@media (prefers-reduced-motion: no-preference) {
  .care-glyph--running .care-glyph-arc {
    transform-origin: 10px 10px;
    animation: care-glyph-turn 1.1s linear infinite;
  }
}
@keyframes care-glyph-turn { to { transform: rotate(360deg); } }
</style>
