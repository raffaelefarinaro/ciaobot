<template>
  <div
    v-if="picker"
    class="newchat-backdrop"
    role="dialog"
    aria-modal="true"
    aria-label="New chat"
    @click.self="cancel"
  >
    <div class="newchat-card" role="listbox" aria-label="Choose a project for the new chat">
      <p class="newchat-title">New chat</p>
      <p class="newchat-hint">{{ hint }}</p>
      <button
        v-for="item in projectItems"
        :key="item.id"
        type="button"
        ref="itemButtons"
        class="newchat-option"
        :class="{ 'newchat-option--active': selected === item.id }"
        :data-workspace-color="item.color"
        role="option"
        :aria-selected="selected === item.id"
        @click="choose(item.id)"
        @mouseenter="selected = item.id"
      >
        <span class="newchat-name">{{ item.label }}</span>
        <span v-if="item.badge" class="newchat-badge">{{ item.badge }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { pendingNewChat } from '../lib/newChat'
import { useProjectStore } from '../stores/projects'
import { workspaceLabel } from '../lib/workspaceLabel'
import { normalizeWorkspaceColor } from '../lib/workspaceColors'

interface PickerItem {
  id: string
  label: string
  color: string
  badge: string
}

const picker = pendingNewChat
const store = useProjectStore()

// The picker opens on the active workspace's projects. Number keys (1-9)
// switch the workspace, which swaps the project list in place.
const projectItems = computed<PickerItem[]>(() => {
  return store.projects
    .filter(p => p.workspace === store.activeWorkspace)
    .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name))
    .map(p => ({
      id: p.project_id,
      label: p.name,
      color: normalizeWorkspaceColor(store.workspaceOptions.find(ws => ws.name === p.workspace)?.color),
      badge: p.is_auto && p.name === 'General' ? 'General' : '',
    }))
})

const selected = ref<string>('')
const itemButtons = ref<HTMLButtonElement[]>([])

const hint = computed(() =>
  `Projects in ${workspaceLabel(store.activeWorkspace)} — press 1-9 to switch workspace.`,
)

function currentIndex(): number {
  return Math.max(0, projectItems.value.findIndex(o => o.id === selected.value))
}

function choose(id: string) {
  picker.value?.resolve(id)
}

function cancel() {
  picker.value?.resolve(null)
}

function focusItem(index: number) {
  nextTick(() => {
    itemButtons.value[index]?.focus()
  })
}

function onKeydown(event: KeyboardEvent) {
  if (!picker.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    cancel()
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    choose(selected.value)
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    const list = projectItems.value
    if (!list.length) return
    const dir = event.key === 'ArrowDown' ? 1 : -1
    const next = (currentIndex() + dir + list.length) % list.length
    selected.value = list[next].id
    focusItem(next)
    return
  }
  if (/^[1-9]$/.test(event.key)) {
    const workspace = store.workspaceOptions[Number(event.key) - 1]
    if (workspace) {
      event.preventDefault()
      store.activeWorkspace = workspace.name
      const projects = projectItems.value
      selected.value = projects[0]?.id ?? ''
      focusItem(0)
    }
  }
}

watch(projectItems, () => {
  const current = projectItems.value.find(o => o.id === selected.value)
  if (!current) selected.value = projectItems.value[0]?.id ?? ''
})

watch(picker, async value => {
  if (!value) return
  selected.value = projectItems.value[0]?.id ?? ''
  await nextTick()
  itemButtons.value[0]?.focus()
})

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  picker.value?.resolve(null)
})
</script>

<style scoped>
.newchat-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgb(0 0 0 / 55%);
}
.newchat-card {
  width: 100%;
  max-width: 26rem;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg2);
  color: var(--fg);
  box-shadow: 0 1.5rem 3rem rgb(0 0 0 / 45%);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.newchat-title {
  margin: 0;
  font-weight: 600;
  font-size: var(--text-lg);
}
.newchat-hint {
  margin: 0 0 var(--space-1);
  color: var(--fg2);
  font-size: var(--text-sm);
}
.newchat-option {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  padding: 0 var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}
.newchat-option:hover {
  background: var(--bg3);
}
.newchat-option--active {
  background: var(--bg3);
  border-color: var(--accent);
}
.newchat-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.newchat-badge {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--fg3);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  flex: 0 0 auto;
}
</style>
