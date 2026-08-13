<template>
  <button
    type="button"
    class="brand wordmark wordmark--sm"
    :class="{ 'brand--refreshing': refreshing }"
    @click="onBrandClick"
    :title="refreshing ? 'Reloading...' : 'Click to reload the latest app build'"
    :aria-busy="refreshing"
  ><span class="brand-label">{{ brandLabel }}</span></button>
</template>

<script setup lang="ts">
// The wordmark, and the click-to-reload behind it. This used to live inline in
// ProjectSidebar's header; it is a component so the pane header can carry the
// mark without the reload logic being copied to a second place.
import { onBeforeUnmount, ref } from 'vue'

const refreshing = ref(false)
const BRAND_TEXT = 'ciaobot'
const PIXEL_CHARS = '█▓▒░▄▀▐▌▆▅▃▂▪▫◆●○·'
const brandLabel = ref(BRAND_TEXT)

let brandPixelTimer: ReturnType<typeof setInterval> | null = null

function startBrandPixelAnimation() {
  stopBrandPixelAnimation()
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  brandPixelTimer = setInterval(() => {
    brandLabel.value = BRAND_TEXT
      .split('')
      .map((char) => (Math.random() < 0.18 ? char : PIXEL_CHARS[Math.floor(Math.random() * PIXEL_CHARS.length)]))
      .join('')
  }, 80)
}

function stopBrandPixelAnimation() {
  if (brandPixelTimer !== null) {
    clearInterval(brandPixelTimer)
    brandPixelTimer = null
  }
  brandLabel.value = BRAND_TEXT
}

onBeforeUnmount(stopBrandPixelAnimation)

async function onBrandClick() {
  if (refreshing.value) return
  refreshing.value = true
  startBrandPixelAnimation()
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
  /* Inherits .wordmark base from App.vue; override size and add interaction.
     --text-lg is the same 16px * font-scale the pane title uses, so the mark
     and the title beside it move together when the font scale changes. */
  font-size: var(--text-lg);
  cursor: pointer;
  transition: opacity 120ms var(--ease);
  min-height: var(--touch);
  align-items: center;
  padding: 0 var(--space-1);
  border: 0;
  background: transparent;
}
.brand::before {
  content: none;
}
.brand:hover { opacity: 0.85; }
.brand:active { opacity: 0.7; }
.brand-label {
  display: inline-block;
  /* Fixed character width so the pixel-jitter animation cannot change the
     mark's width - in the pane header that would shift the centred column. */
  min-width: 7ch;
  text-align: left;
}
.brand--refreshing {
  opacity: 1;
  color: var(--accent);
  pointer-events: none;
}
.brand--refreshing .brand-label {
  animation: brand-pixel-jitter 0.12s steps(2, end) infinite;
  text-shadow:
    1px 0 color-mix(in srgb, var(--accent) 75%, transparent),
    -1px 0 color-mix(in srgb, var(--accent2) 55%, transparent),
    0 1px color-mix(in srgb, var(--fg) 35%, transparent);
}
@keyframes brand-pixel-jitter {
  0%, 100% { transform: translate(0, 0); }
  50% { transform: translate(1px, -1px); }
}
@media (prefers-reduced-motion: reduce) {
  .brand--refreshing .brand-label { animation: none; }
}
</style>
