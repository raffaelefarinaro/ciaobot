<script setup lang="ts">
// The one footer of a setup screen. Sticky inside the scrolling shell, with the
// start slot (Back) on the left and the end slot (Leave, Continue) on the right.
</script>

<template>
  <footer class="ob-footer">
    <div class="ob-footer-row"><slot name="start" /></div>
    <div class="ob-footer-row"><slot /></div>
  </footer>
</template>

<style scoped>
.ob-footer { position: sticky; bottom: 0; z-index: 1; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-3, 12px); margin-top: var(--space-6, 32px); padding: var(--space-4, 16px) 0; border-top: 1px solid var(--border); background: var(--bg); }
.ob-footer-row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3, 12px); }
/* One row on a 360px phone: tighter gaps keep Back, Leave and Continue/Finish together. */
@media (max-width: 560px) { .ob-footer, .ob-footer-row { flex-wrap: nowrap; gap: var(--space-2, 8px); } }
</style>
