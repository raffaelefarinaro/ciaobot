<template>
  <img
    class="wordmark"
    :class="`wordmark--${size}`"
    :src="src"
    width="1402"
    height="324"
    alt="Ciaobot"
    decoding="async"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { isLightTheme } from '../lib/theme'

withDefaults(defineProps<{
  size?: 'lg' | 'md' | 'sm'
}>(), {
  size: 'md',
})

// One image per render, chosen by the theme: the ink artwork reads on light
// backgrounds, the off-white artwork on dark ones.
const src = computed(() =>
  isLightTheme.value
    ? `${import.meta.env.BASE_URL}brand/ciaobot-wordmark.png`
    : `${import.meta.env.BASE_URL}brand/ciaobot-wordmark-light.png`,
)
</script>

<style scoped>
/* The sizes live here only. width/height above are the artwork's intrinsic
   1402x324 ratio, so the box reserves space before the image loads. */
.wordmark {
  display: inline-block;
  vertical-align: middle;
  width: auto;
  height: auto;
  max-width: 100%;
  object-fit: contain;
  user-select: none;
}
.wordmark--lg { max-height: 28px; }
.wordmark--md { max-height: 22px; }
.wordmark--sm { max-height: 16px; }
</style>
