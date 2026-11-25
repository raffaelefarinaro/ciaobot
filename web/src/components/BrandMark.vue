<template>
  <button
    type="button"
    class="brand"
    :title="refreshing ? 'Reloading...' : 'Click to reload the latest app build'"
    aria-label="Reload Ciaobot"
    :aria-busy="refreshing"
    @pointerenter="move(1)"
    @pointerleave="move(0)"
    @focus="move(1)"
    @blur="move(0)"
    @click="reload"
  >
    <CiaoMark :progress="progress" />
  </button>
</template>

<script setup lang="ts">
// The product mark, and the hard refresh behind it. Hovering (or focusing) it
// closes the C into a ring; leaving opens it again from wherever it got to, so
// the motion can be stopped midway and reversed.
import { onBeforeUnmount, ref } from 'vue'
import CiaoMark from './CiaoMark.vue'

// Share of the remaining distance covered per frame: slow enough to read as a
// slow close, and to leave room to turn around midway.
const FOLLOW = 0.04

const progress = ref(0)
const refreshing = ref(false)
let target = 0
let frame = 0

function reducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function tick() {
  const next = progress.value + (target - progress.value) * FOLLOW
  progress.value = Math.abs(target - next) < 0.0005 ? target : next
  frame = progress.value === target ? 0 : requestAnimationFrame(tick)
}

function move(to: number) {
  if (reducedMotion()) {
    cancelAnimationFrame(frame)
    frame = 0
    progress.value = target = 0
    return
  }
  target = to
  if (!frame) frame = requestAnimationFrame(tick)
}

onBeforeUnmount(() => cancelAnimationFrame(frame))

async function reload() {
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
  // Bust the HTTP cache too via a query string; replace so no back-button stale entry.
  const url = new URL(window.location.href)
  url.searchParams.set('_r', String(Date.now()))
  window.location.replace(url.toString())
}
</script>

<style scoped>
.brand {
  display: block;
  box-sizing: content-box;
  width: 30px;
  height: 30px;
  flex: none;
  /* A 44px touch target around the 30px mark without moving it. */
  padding: calc((var(--touch) - 30px) / 2);
  margin: calc((30px - var(--touch)) / 2);
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
</style>
