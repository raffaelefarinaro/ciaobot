<template>
  <div class="toast-stack" v-if="store.toasts.length">
    <div
      v-for="t in store.toasts"
      :key="t.id"
      class="toast"
      role="status"
      @click="onClick(t)"
    >
      <div class="toast-title">{{ t.title }}</div>
      <div class="toast-body">{{ t.body }}</div>
      <button
        class="toast-close"
        @click.stop="store.dismissToast(t.id)"
        aria-label="Dismiss"
      >&times;</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useProjectStore } from '../stores/projects'
import type { InAppToast } from '../lib/types'

const store = useProjectStore()

async function onClick(toast: InAppToast) {
  // Switch workspace if needed (the toast may be for a chat in the other one)
  const project = store.projectFor(toast.chat_id)
  if (project && project.workspace !== store.activeWorkspace) {
    await store.switchWorkspace(project.workspace)
  }
  await store.switchChat(toast.chat_id)
  store.dismissToast(toast.id)
}
</script>

<style scoped>
.toast-stack {
  position: fixed;
  top: calc(12px + var(--safe-top));
  right: calc(12px + var(--safe-right));
  z-index: 200;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(360px, calc(100vw - 24px));
  pointer-events: none;
}

.toast {
  background: var(--bg-elev);
  color: var(--fg);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  padding: 10px 32px 10px 12px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  cursor: pointer;
  position: relative;
  pointer-events: auto;
  animation: toast-in 180ms var(--ease);
}

.toast:hover { background: var(--bg3); }

.toast-title {
  font-size: var(--text-base);
  font-weight: 600;
  margin-bottom: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.toast-body {
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.toast-close {
  position: absolute;
  top: 4px;
  right: 4px;
  background: transparent;
  border: none;
  color: var(--fg2);
  font-size: 18px;
  line-height: 1;
  width: 24px;
  height: 24px;
  cursor: pointer;
  border-radius: var(--radius-sm);
}
.toast-close:hover { color: var(--fg); background: var(--bg2); }

@keyframes toast-in {
  from { opacity: 0; transform: translateY(-8px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 600px) {
  .toast-stack {
    left: calc(12px + var(--safe-left));
    right: calc(12px + var(--safe-right));
    max-width: none;
  }
}
</style>
