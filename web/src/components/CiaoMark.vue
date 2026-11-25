<template>
  <svg class="ciao-mark" viewBox="260 250 720 720" aria-hidden="true">
    <g :transform="`rotate(${shape.rotate} ${RING_CENTRE.x} ${RING_CENTRE.y})`">
      <path d="M718.7 334.7A295 295 0 1 0 768.5 876.5" fill="none" stroke="currentColor" stroke-width="110" stroke-linecap="round" />
      <path v-if="shape.arcA" :d="shape.arcA" fill="none" stroke="var(--accent)" stroke-width="110" stroke-linecap="round" />
      <path v-if="shape.arcB" :d="shape.arcB" fill="none" stroke="var(--accent)" stroke-width="110" stroke-linecap="round" />
      <circle :cx="shape.dotA.cx" :cy="shape.dotA.cy" :r="shape.dotA.r" fill="var(--accent)" />
      <circle :cx="shape.dotB.cx" :cy="shape.dotB.cy" :r="shape.dotB.r" fill="var(--accent)" />
    </g>
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RING_CENTRE, ringShape } from '../lib/ciaoRing'

// 0 is the resting C with its two dots, 1 is the closed ring.
const props = withDefaults(defineProps<{ progress?: number }>(), { progress: 0 })
const shape = computed(() => ringShape(props.progress))
</script>

<style scoped>
.ciao-mark {
  display: block;
  width: 100%;
  height: 100%;
  overflow: visible;
}
</style>
