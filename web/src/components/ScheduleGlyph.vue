<template>
  <svg class="schedule-glyph" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true" focusable="false">
    <circle cx="6" cy="6" r="5.2" />
    <path d="M6 3.2V6l1.9 1.3" stroke-linecap="round" stroke-linejoin="round" />
  </svg>
</template>

<style scoped>
/* The scheduled-chat mark, drawn like TaskGlyph: decorative, the caller owns the name. */
.schedule-glyph {
  display: block;
  width: calc(12px * var(--font-scale));
  height: calc(12px * var(--font-scale));
  flex: 0 0 auto;
}
</style>
