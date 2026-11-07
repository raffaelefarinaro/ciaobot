<script setup lang="ts">
// The one button for setup screens: 44px target, tokens only. Attributes
// (disabled, class, aria-*) fall through to the native button.
withDefaults(defineProps<{ variant?: 'primary' | 'secondary' | 'link' }>(), { variant: 'secondary' })
</script>

<template>
  <button type="button" class="ob-button" :data-variant="variant"><slot /></button>
</template>

<style scoped>
.ob-button { min-height: var(--touch, 44px); padding: var(--space-2, 8px) var(--space-4, 16px); border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: transparent; color: var(--fg); font: inherit; cursor: pointer; }
.ob-button[data-variant="primary"] { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.ob-button[data-variant="link"] { border-color: transparent; color: var(--accent); }
.ob-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ob-button:disabled { opacity: .55; cursor: not-allowed; }
</style>
