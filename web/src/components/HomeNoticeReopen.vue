<script setup lang="ts">
import { useHomeNoticeWindows } from '../composables/useHomeNoticeWindows'

const notices = useHomeNoticeWindows()
</script>

<template>
  <button
    v-if="notices.closedCount.value"
    type="button"
    class="home-notice-reopen btn-chip btn-small"
    data-home-notices-reopen
    @click="notices.reopenAll()"
  >Show {{ notices.closedCount.value }} closed {{ notices.closedCount.value === 1 ? 'notice' : 'notices' }}</button>
</template>

<style scoped>
.home-notice-reopen { align-self: flex-start; margin-top: var(--space-3); }
@media (pointer: coarse) { .home-notice-reopen { min-height: 44px; } }
</style>
