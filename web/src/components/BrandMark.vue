<template>
  <button
    type="button"
    class="brand"
    :class="{ 'brand--refreshing': refreshing }"
    @click="onBrandClick"
    :title="refreshing ? 'Reloading...' : 'Click to reload the latest app build'"
    aria-label="Reload Ciaobot"
    :aria-busy="refreshing"
  ><img class="brand-label brand-logo--dark" src="/brand/ciaobot-wordmark-light.png" alt=""><img class="brand-label brand-logo--light" src="/brand/ciaobot-wordmark.png" alt=""></button>
</template>

<script setup lang="ts">
// The wordmark, and the click-to-reload behind it. This used to live inline in
// ProjectSidebar's header; it is a component so the pane header can carry the
// mark without the reload logic being copied to a second place.
import { ref } from 'vue'

const refreshing = ref(false)
async function onBrandClick() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    // Force the service worker to update without unregistering it,
    // so push subscriptions survive across builds.
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations()
      await Promise.all(regs.map(r => r.update()))
    }
    if (typeof caches !== 'undefined') {
      const keys = await caches.keys()
      await Promise.all(keys.map(k => caches.delete(k)))
    }
  } catch (e) {
    console.warn('Hard refresh cleanup failed', e)
  }
  // Bust HTTP cache too via a query string; replace so no back-button stale entry.
  const url = new URL(window.location.href)
  url.searchParams.set('_r', String(Date.now()))
  window.location.replace(url.toString())
}
</script>

<style scoped>
.brand {
  display: inline-flex;
  cursor: pointer;
  transition: opacity 120ms var(--ease);
  min-height: var(--touch);
  align-items: center;
  padding: 0 var(--space-1);
  border: 0;
  background: transparent;
}
.brand:hover { opacity: 0.85; }
.brand:active { opacity: 0.7; }
.brand-label {
  display: block;
  width: calc(5.5 * var(--text-lg));
  height: auto;
}
.brand-logo--light { display: none; }
:global(.theme-light .brand-logo--dark) { display: none; }
:global(.theme-light .brand-logo--light) { display: block; }
.brand--refreshing {
  opacity: 1;
  pointer-events: none;
}
.brand--refreshing .brand-label {
  animation: brand-pixel-jitter 0.12s steps(2, end) infinite;
}
@keyframes brand-pixel-jitter {
  0%, 100% { transform: translate(0, 0); }
  50% { transform: translate(1px, -1px); }
}
@media (prefers-reduced-motion: reduce) {
  .brand--refreshing .brand-label { animation: none; }
}
</style>
