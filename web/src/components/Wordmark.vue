<template>
  <span class="wordmark" :class="`wordmark--${size}`" role="img" :aria-label="text">
    <template v-for="(character, index) in characters" :key="index">
      <span v-if="/o/i.test(character)" class="wordmark-dot" aria-hidden="true"></span>
      <span v-else aria-hidden="true">{{ character }}</span>
    </template>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  text?: string
  size?: 'lg' | 'md' | 'sm'
}>(), {
  text: 'Ciaobot',
  size: 'md',
})

const characters = computed(() => Array.from(props.text))
</script>
