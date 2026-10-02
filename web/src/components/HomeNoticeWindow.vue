<script setup lang="ts">
import { nextTick } from 'vue'
import { useHomeNoticeWindows } from '../composables/useHomeNoticeWindows'

defineProps<{ title: string; noticeKey: string; blocking?: boolean }>()
const notices = useHomeNoticeWindows()

async function close(key: string) {
  notices.close(key)
  await nextTick()
  // Closing removes the focused button. Leave keyboard users at the explicit
  // recovery control instead of sending the next Tab to the page start.
  document.querySelector<HTMLButtonElement>('[data-home-notices-reopen]')?.focus()
}
</script>

<template>
  <article class="home-notice-window" :class="{ 'home-notice-window--blocking': blocking }" data-home-notice-window>
    <header class="home-notice-header">
      <h2 class="home-notice-title">{{ title }}</h2>
      <button type="button" class="btn-icon home-notice-close" :aria-label="`Close ${title} window`" :title="`Close ${title} window`" @click="close(noticeKey)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
      </button>
    </header>
    <div class="home-notice-content"><slot /></div>
  </article>
</template>

<style scoped>
.home-notice-window {
  min-width: 0;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: 0 12px 32px -14px rgb(0 0 0 / 45%);
  overflow: hidden;
}
.home-notice-window--blocking { border-color: var(--warning); }
.home-notice-header {
  min-height: 52px;
  padding: 0 var(--space-2) 0 var(--space-3);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  border-bottom: 1px solid var(--border);
}
.home-notice-title {
  min-width: 0;
  margin: 0;
  font-size: var(--text-base);
  font-weight: 650;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.home-notice-close {
  flex: 0 0 auto;
  min-width: 44px;
  min-height: 44px;
}
.home-notice-content { padding: var(--space-3); }
:global(:root.theme-light) .home-notice-window { box-shadow: 0 10px 28px -14px rgb(26 26 46 / 28%); }
</style>
