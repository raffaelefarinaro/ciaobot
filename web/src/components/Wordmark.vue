<template>
  <span class="wordmark" :class="`wordmark--${size}`" role="img" aria-label="Ciaobot">
    <!-- Both images are the supplied brand artwork. The theme class on <html>
         picks the one that reads on the current background; the hidden one
         leaves the accessibility tree with display: none. -->
    <img
      class="wordmark-img wordmark-img--on-dark"
      :src="onDarkSrc"
      :width="width"
      :height="height"
      alt=""
      aria-hidden="true"
      decoding="async"
    />
    <img
      class="wordmark-img wordmark-img--on-light"
      :src="onLightSrc"
      :width="width"
      :height="height"
      alt=""
      aria-hidden="true"
      decoding="async"
    />
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  size?: 'lg' | 'md' | 'sm'
}>(), {
  size: 'md',
})

// Rendered heights per size; the artwork is 1402x324 px, so width follows the ratio.
const HEIGHTS = { lg: 28, md: 22, sm: 16 } as const
const ARTWORK_WIDTH = 1402
const ARTWORK_HEIGHT = 324

const height = computed(() => HEIGHTS[props.size])
const width = computed(() => Math.round((height.value * ARTWORK_WIDTH) / ARTWORK_HEIGHT))

// The app is dark by default; `theme-light` on <html> switches to the light theme.
const onDarkSrc = `${import.meta.env.BASE_URL}brand/ciaobot-wordmark-light.png`
const onLightSrc = `${import.meta.env.BASE_URL}brand/ciaobot-wordmark.png`
</script>

<style scoped>
.wordmark {
  display: inline-flex;
  align-items: center;
  line-height: 0;
  max-width: 100%;
  user-select: none;
}
.wordmark--lg .wordmark-img { height: 28px; }
.wordmark--md .wordmark-img { height: 22px; }
.wordmark--sm .wordmark-img { height: 16px; }
.wordmark-img {
  width: auto;
  max-width: 100%;
}

.wordmark-img--on-light { display: none; }
:global(.theme-light) .wordmark-img--on-dark { display: none; }
:global(.theme-light) .wordmark-img--on-light { display: block; }
</style>
