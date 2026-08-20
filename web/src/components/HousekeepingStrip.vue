<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useHousekeepingStore } from '../stores/housekeeping'
import { useProjectStore } from '../stores/projects'
import type { OperatorAction } from '../lib/types'

const housekeeping = useHousekeepingStore()
const projectStore = useProjectStore()

const chatBusy = ref(false)

onMounted(() => {
  housekeeping.init()
})

// The strip has no permanent furniture: with zero actions it renders nothing at
// all (the template guards on a non-empty list before creating any element).
function hasActions(): boolean {
  return housekeeping.actions.length > 0
}

async function runAction(action: OperatorAction): Promise<void> {
  await housekeeping.run(action.id)
}

async function openChat(action: OperatorAction): Promise<void> {
  if (chatBusy.value || !action.chat_prompt) return
  chatBusy.value = true
  try {
    const workspace = action.workspace || projectStore.activeWorkspace
    if (projectStore.activeWorkspace !== workspace) {
      await projectStore.switchWorkspace(workspace)
    }
    let project = projectStore.projects.find(
      p => p.workspace === workspace && p.is_auto,
    )
    if (!project) {
      project = await projectStore.createProject('General')
    }
    const chat = await projectStore.createChat(project.project_id, 'Housekeeping')
    if (chat) {
      projectStore.sendMessage(chat.chat_id, action.chat_prompt)
      const { router } = await import('../router')
      router.push(`/chat/${chat.chat_id}`)
    }
  } finally {
    chatBusy.value = false
  }
}
</script>

<template>
  <section v-if="hasActions()" class="housekeeping" aria-label="Housekeeping actions">
    <article
      v-for="action in housekeeping.actions"
      :key="action.id"
      class="housekeeping-tile"
    >
      <span class="housekeeping-glyph" aria-hidden="true">{{ action.glyph }}</span>
      <div class="housekeeping-body">
        <p class="housekeeping-title">{{ action.title }}</p>
        <p class="housekeeping-detail">{{ action.detail }}</p>
      </div>
      <div class="housekeeping-actions">
        <button
          v-if="action.run_label"
          type="button"
          class="btn-small btn-primary"
          :disabled="housekeeping.runningIds.has(action.id)"
          @click="runAction(action)"
        >
          {{ housekeeping.runningIds.has(action.id) ? 'Running…' : action.run_label }}
        </button>
        <button
          v-if="action.chat_prompt"
          type="button"
          class="btn-small btn-chip"
          :disabled="chatBusy"
          @click="openChat(action)"
        >
          {{ action.chat_label || 'Discuss in chat' }}
        </button>
      </div>
    </article>
  </section>
</template>

<style scoped>
.housekeeping {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.housekeeping-tile {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  min-width: 0;
}

.housekeeping-glyph {
  font-size: var(--text-base);
  color: var(--warn);
  line-height: 1.4;
}

.housekeeping-body {
  flex: 1 1 auto;
  min-width: 0;
}

.housekeeping-title {
  margin: 0;
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
}

.housekeeping-detail {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

.housekeeping-actions {
  display: flex;
  gap: var(--space-1);
  flex-shrink: 0;
}
</style>
