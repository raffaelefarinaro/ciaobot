<template>
  <header class="pane-header">
    <button class="header-hamburger touch-hit" aria-label="Open sidebar" @click="$emit('open-sidebar')">
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
    <div v-if="$slots.actions" class="header-actions" :key="activeBgAgents">
      <slot name="actions" />
    </div>
    <NotificationBell class="header-bell" />
  </header>
</template>

<script setup lang="ts">
import NotificationBell from './NotificationBell.vue'

defineProps<{ title?: string; activeBgAgents?: number }>()
defineEmits<{ 'open-sidebar': [] }>()
</script>

<style scoped>
.pane-header {
  display: flex;
  align-items: center;
  /* Match the sidebar header: 44px controls + 8px vertical padding + border. */
  height: calc(61px + var(--safe-top));
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
:deep(.header-left) {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
  text-align: left;
}
.header-title h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.pane-title) {
  font-weight: 600;
  font-size: 16px;
  display: block;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
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
  width: 30px;
  height: 30px;
  background: none;
  border: none;
  color: var(--fg);
  cursor: pointer;
  border-radius: 8px;
  flex-shrink: 0;
}
.header-hamburger:active { transform: scale(0.96); }
.header-bell {
  flex-shrink: 0;
  display: none;
}
/* Unify header icon sizes with the sidebar (30px containers, 18px content).
   ::before keeps hover/active fills at the 30px visual footprint. */
.pane-header :deep(.btn-icon),
.pane-header :deep(.model-picker-btn),
.pane-header :deep(.archive-btn) {
  box-sizing: content-box;
  width: 30px;
  height: 30px;
  min-width: 30px;
  min-height: 30px;
  padding: calc((var(--touch) - 30px) / 2);
  margin: calc((30px - var(--touch)) / 2);
  border-radius: 6px;
  position: relative;
  isolation: isolate;
}
.pane-header :deep(.btn-icon::before),
.pane-header :deep(.model-picker-btn::before),
.pane-header :deep(.archive-btn::before),
.pane-header .header-bell :deep(.bell-btn::before) {
  content: '';
  position: absolute;
  inset: calc((var(--touch) - 30px) / 2);
  z-index: -1;
  border-radius: 6px;
  background: transparent;
  pointer-events: none;
  transition: background 120ms var(--ease);
}
.pane-header :deep(.btn-icon:hover),
.pane-header :deep(.model-picker-btn:hover),
.pane-header :deep(.archive-btn:hover),
.pane-header .header-bell :deep(.bell-btn:hover) {
  background: transparent;
}
.pane-header :deep(.btn-icon:hover::before),
.pane-header :deep(.model-picker-btn:hover::before),
.pane-header :deep(.archive-btn:hover::before),
.pane-header .header-bell :deep(.bell-btn:hover::before) {
  background: var(--bg3);
}
.pane-header :deep(.btn-icon.active::before),
.pane-header :deep(.btn-icon[aria-pressed="true"]::before) {
  background: var(--bg3);
  border: 1px solid var(--border);
}
.pane-header :deep(.bell-btn) {
  box-sizing: content-box;
  width: 30px;
  height: 30px;
  padding: calc((var(--touch) - 30px) / 2);
  margin: calc((30px - var(--touch)) / 2);
}
.pane-header :deep(.bell-btn) svg {
  width: 18px;
  height: 18px;
}
.pane-header .header-bell :deep(.bell-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 6px;
  background: transparent;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  transition: color 120ms var(--ease), transform 120ms var(--ease);
}
.pane-header .header-bell :deep(.bell-btn:hover) {
  color: var(--fg);
}
.pane-header .header-bell :deep(.bell-btn:active) { transform: scale(0.96); }
.pane-header .header-bell :deep(.bell-btn.has-unread) { color: var(--accent); }
.pane-header :deep(.btn-icon:active),
.pane-header :deep(.model-picker-btn:active),
.pane-header :deep(.archive-btn:active) {
  transform: scale(0.96);
  background: transparent;
}
@media (max-width: 768px) {
  .pane-header {
    height: auto;
    padding-left: calc(12px + var(--safe-left));
    padding-right: calc(12px + var(--safe-right));
  }
  .header-hamburger,
  .header-bell { display: flex; }
  .header-title { text-align: left; min-width: 0; }
  :deep(.header-left) { min-width: 0; }
  :deep(.header-actions) {
    flex-shrink: 0;
    gap: 6px;
  }
  :deep(.pane-title) {
    flex: 1 1 100%;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    font-size: 12px;
    line-height: 1.2;
    white-space: normal;
  }
}
</style>
