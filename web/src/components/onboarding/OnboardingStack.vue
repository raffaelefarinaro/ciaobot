<script setup lang="ts">
// Vertical rhythm for the setup stages: related items sit `md` apart, groups
// `xl` apart and sections `2xl` apart (DESIGN.md spacing tokens).
withDefaults(defineProps<{ gap?: 'sm' | 'md' | 'xl' | '2xl'; as?: string }>(), { gap: 'md', as: 'div' })
</script>

<template>
  <component :is="as" class="ob-stack" :data-gap="gap"><slot /></component>
</template>

<style scoped>
.ob-stack { display: grid; min-width: 0; }
.ob-stack[data-gap="sm"] { gap: var(--space-2, 8px); }
.ob-stack[data-gap="md"] { gap: var(--space-3, 12px); }
.ob-stack[data-gap="xl"] { gap: var(--space-5, 24px); }
.ob-stack[data-gap="2xl"] { gap: var(--space-6, 32px); }
.ob-stack > :deep(h2), .ob-stack > :deep(h3), .ob-stack > :deep(p) { margin: 0; }
</style>
