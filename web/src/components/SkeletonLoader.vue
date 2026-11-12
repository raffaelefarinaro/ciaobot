<script setup lang="ts">
/**
 * The first-load placeholder every list-shaped page shares.
 *
 * A page that has not answered yet draws the shape it is about to fill — rows,
 * cards, or the task board's columns — instead of a sentence, so the layout
 * does not jump when the data lands. It is one status for assistive tech: the
 * shapes are hidden, and `label` is what a screen reader hears ("Loading
 * tasks"). Reduced motion keeps a slow pulse rather than the sweep, so the
 * placeholder still reads as "in progress" rather than as empty boxes.
 *
 * Only for a first load. A refresh over data already on screen keeps the data;
 * that is each page's own load-state rule, and this component does not change it.
 */
withDefaults(
  defineProps<{
    /** What is loading, as a screen reader should hear it. */
    label: string
    /**
     * `rows`: a list. `cards`: stacked cards. `board`: four columns of cards.
     * `graph`: nodes and links, for the Memory Map's drawing.
     */
    variant?: 'rows' | 'cards' | 'board' | 'graph'
    /** How many rows or cards (per column, for `board`). */
    count?: number
  }>(),
  { variant: 'rows', count: 4 },
)

/** Fixed, varied widths so the placeholder reads as text, not as bars. */
const WIDTHS = ['92%', '74%', '86%', '61%', '80%', '68%']
</script>

<template>
  <div class="skeleton" :class="`skeleton--${variant}`" role="status" aria-live="polite" :aria-label="label">
    <template v-if="variant === 'board'">
      <div v-for="column in 4" :key="column" class="skeleton-column" aria-hidden="true">
        <span class="skeleton-line skeleton-line--heading"></span>
        <div v-for="i in Math.max(1, count - (column % 3))" :key="i" class="skeleton-card">
          <span class="skeleton-line" :style="{ width: WIDTHS[(i + column) % WIDTHS.length] }"></span>
          <span class="skeleton-line skeleton-line--short"></span>
        </div>
      </div>
    </template>
    <div v-else-if="variant === 'graph'" class="skeleton-graph-wrap" aria-hidden="true">
      <svg viewBox="0 0 200 140" class="skeleton-graph">
        <line x1="45" y1="38" x2="82" y2="52" class="skeleton-edge" />
        <line x1="82" y1="52" x2="118" y2="42" class="skeleton-edge" />
        <line x1="118" y1="42" x2="155" y2="58" class="skeleton-edge" />
        <line x1="82" y1="52" x2="92" y2="92" class="skeleton-edge" />
        <line x1="118" y1="42" x2="108" y2="92" class="skeleton-edge" />
        <line x1="92" y1="92" x2="108" y2="92" class="skeleton-edge" />
        <line x1="45" y1="38" x2="38" y2="86" class="skeleton-edge" />
        <line x1="38" y1="86" x2="70" y2="118" class="skeleton-edge" />
        <line x1="155" y1="58" x2="162" y2="92" class="skeleton-edge" />
        <line x1="162" y1="92" x2="130" y2="118" class="skeleton-edge" />
        <line x1="70" y1="118" x2="100" y2="128" class="skeleton-edge" />
        <line x1="130" y1="118" x2="100" y2="128" class="skeleton-edge" />
        <circle cx="45" cy="38" r="13" class="skeleton-node skeleton-node--1" />
        <circle cx="82" cy="52" r="10" class="skeleton-node skeleton-node--2" />
        <circle cx="118" cy="42" r="11" class="skeleton-node skeleton-node--3" />
        <circle cx="155" cy="58" r="12" class="skeleton-node skeleton-node--4" />
        <circle cx="92" cy="92" r="11" class="skeleton-node skeleton-node--2" />
        <circle cx="108" cy="92" r="10" class="skeleton-node skeleton-node--1" />
        <circle cx="38" cy="86" r="9" class="skeleton-node skeleton-node--3" />
        <circle cx="162" cy="92" r="9" class="skeleton-node skeleton-node--2" />
        <circle cx="70" cy="118" r="10" class="skeleton-node skeleton-node--4" />
        <circle cx="130" cy="118" r="10" class="skeleton-node skeleton-node--1" />
        <circle cx="100" cy="128" r="9" class="skeleton-node skeleton-node--3" />
      </svg>
    </div>
    <template v-else-if="variant === 'cards'">
      <div v-for="i in count" :key="i" class="skeleton-card" aria-hidden="true">
        <span class="skeleton-line skeleton-line--heading" :style="{ width: WIDTHS[i % WIDTHS.length] }"></span>
        <span class="skeleton-line"></span>
        <span class="skeleton-line" :style="{ width: WIDTHS[(i + 2) % WIDTHS.length] }"></span>
      </div>
    </template>
    <template v-else>
      <div v-for="i in count" :key="i" class="skeleton-row" aria-hidden="true">
        <span class="skeleton-line" :style="{ width: WIDTHS[i % WIDTHS.length] }"></span>
        <span class="skeleton-line skeleton-line--short"></span>
      </div>
    </template>
    <span class="sr-only">{{ label }}…</span>
  </div>
</template>

<style scoped>
.skeleton {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) 0;
}
.skeleton--board {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-3);
  align-items: start;
}
@container chat-pane (max-width: 940px) {
  .skeleton--board { grid-template-columns: minmax(0, 1fr); }
  .skeleton--board .skeleton-column:nth-child(n + 2) { display: none; }
}
.skeleton--graph {
  flex: 1;
  align-items: center;
  justify-content: center;
  min-height: 320px;
  padding: var(--space-5);
}
.skeleton-graph-wrap { width: min(360px, 80%); }
.skeleton-graph { display: block; width: 100%; height: auto; }
.skeleton-edge { stroke: var(--border); stroke-width: 2; stroke-linecap: round; opacity: 0.55; }
.skeleton-node { fill: var(--bg3); stroke: var(--border); stroke-width: 1.6; animation: skeleton-node-pulse 1.6s ease-in-out infinite; }
.skeleton-node--2 { animation-delay: 0.2s; }
.skeleton-node--3 { animation-delay: 0.4s; }
.skeleton-node--4 { animation-delay: 0.6s; }
@keyframes skeleton-node-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}
.skeleton-column {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
}
.skeleton-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
}
.skeleton-row {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--border);
}
.skeleton-line {
  display: block;
  width: 100%;
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--bg2) 0%, var(--bg3) 50%, var(--bg2) 100%);
  background-size: 200% 100%;
  animation: skeleton-sweep 1.4s ease-in-out infinite;
}
.skeleton-card .skeleton-line {
  background-image: linear-gradient(90deg, var(--bg3) 0%, var(--border-strong) 50%, var(--bg3) 100%);
}
.skeleton-line--heading { width: 42%; height: 12px; }
.skeleton-line--short { width: 34%; }
@keyframes skeleton-sweep {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}
@media (prefers-reduced-motion: reduce) {
  .skeleton-line { animation: skeleton-pulse 1.8s ease-in-out infinite; }
  .skeleton-node { animation-duration: 1.8s; animation-delay: 0s; }
}
@keyframes skeleton-pulse {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}
</style>
