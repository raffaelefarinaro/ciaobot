<template>
  <header class="pane-header">
    <button class="header-hamburger" aria-label="Open sidebar" @click="$emit('open-sidebar')">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
        <line x1="4" y1="7" x2="20" y2="7"/>
        <line x1="4" y1="12" x2="20" y2="12"/>
        <line x1="4" y1="17" x2="20" y2="17"/>
      </svg>
    </button>
    <div class="header-title">
      <slot name="title">
        <h2>{{ title }}</h2>
      </slot>
    </div>
    <div v-if="$slots.actions" class="header-actions">
      <slot name="actions" />
    </div>
    <NotificationBell class="header-bell" />
  </header>
</template>

<script setup lang="ts">
import NotificationBell from './NotificationBell.vue'

defineProps<{ title?: string }>()
defineEmits<{ 'open-sidebar': [] }>()
</script>

<style scoped>
.pane-header {
  display: flex;
  align-items: center;
  height: calc(46px + var(--safe-top));
  padding: calc(8px + var(--safe-top)) 8px 8px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  gap: 8px;
  flex-shrink: 0;
  box-sizing: border-box;
}
.header-title {
  flex: 1;
  min-width: 0;
  text-align: center;
}
.header-title h2 {
  margin: 0;
  font-size: 16px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
.header-hamburger {
  display: none;
  align-items: center;
  justify-content: center;
  width: var(--touch);
  height: var(--touch);
  background: none;
  border: none;
  color: var(--fg);
  cursor: pointer;
  border-radius: 8px;
  flex-shrink: 0;
}
.header-hamburger:active { background: var(--bg3); }
.header-bell {
  flex-shrink: 0;
  display: none;
}
/* Unify header icon sizes with the sidebar (30px containers, 18px content). */
:deep(.btn-icon),
:deep(.model-picker-btn) {
  min-width: 30px;
  min-height: 30px;
  padding: 5px;
  border-radius: 6px;
}
:deep(.bell-btn) {
  width: 30px;
  height: 30px;
}
:deep(.bell-btn) svg {
  width: 18px;
  height: 18px;
}
.header-bell :deep(.bell-btn) {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
}
.header-bell :deep(.bell-btn.has-unread) { color: var(--accent); }
@media (max-width: 768px) {
  .pane-header {
    height: auto;
  }
  .header-hamburger,
  .header-bell { display: flex; }
  :deep(.btn-icon),
  :deep(.model-picker-btn) {
    min-width: var(--touch);
    min-height: var(--touch);
    padding: 8px;
    border-radius: var(--radius);
  }
  :deep(.bell-btn) {
    width: var(--touch);
    height: var(--touch);
  }
  :deep(.bell-btn) svg {
    width: 20px;
    height: 20px;
  }
}
</style>
