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
    /** `rows`: a list. `cards`: stacked cards. `board`: four columns of cards. */
    variant?: 'rows' | 'cards' | 'board'
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
}
@keyframes skeleton-pulse {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}
</style>
