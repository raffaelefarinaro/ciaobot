<template>
  <svg class="task-glyph" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true" focusable="false">
    <rect x="0.8" y="0.8" width="10.4" height="10.4" rx="2.4" />
    <path d="M3.4 6.2l1.8 1.8 3.4-3.6" stroke-linecap="round" stroke-linejoin="round" />
  </svg>
</template>

<style scoped>
/* The task-chat mark (DESIGN.md, Task board): decorative, the caller owns the name. */
.task-glyph {
  display: block;
  width: calc(12px * var(--font-scale));
  height: calc(12px * var(--font-scale));
  flex: 0 0 auto;
}
</style>
