<template>
  <div
    v-if="picker"
    class="newchat-backdrop"
    role="dialog"
    aria-modal="true"
    aria-label="New chat"
    @click.self="cancel"
  >
    <div class="newchat-card" role="listbox" aria-label="Choose a workspace for the new chat">
      <p class="newchat-title">New chat</p>
      <p class="newchat-hint">Create in General, or pick a workspace below.</p>
      <button
        v-for="option in options"
        :key="option.workspace"
        type="button"
        ref="optionButtons"
        class="newchat-option"
        :class="{ 'newchat-option--active': selected === option.workspace }"
        :data-workspace-color="option.color"
        role="option"
        :aria-selected="selected === option.workspace"
        @click="create(option.workspace)"
        @mouseenter="selected = option.workspace"
      >
        <span v-if="indexOf(option.workspace) < 9" class="newchat-key" aria-hidden="true">{{ indexOf(option.workspace) + 1 }}</span>
        <span v-else class="newchat-key newchat-key--empty" aria-hidden="true">·</span>
        <span class="newchat-name">{{ option.label }}</span>
        <span v-if="option.isGeneral" class="newchat-badge">General</span>
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

interface NewChatOption {
  workspace: string
  label: string
  color: string
  isGeneral: boolean
}

const picker = pendingNewChat
const store = useProjectStore()

const options = computed<NewChatOption[]>(() => {
  return store.workspaceOptions.map(ws => ({
    workspace: ws.name,
    label: workspaceLabel(ws.name),
    color: normalizeWorkspaceColor(ws.color),
    isGeneral: store.projects.some(p => p.workspace === ws.name && p.is_auto && p.name === 'General'),
  }))
})

const selected = ref<string>('')
const optionButtons = ref<HTMLButtonElement[]>([])

function indexOf(workspace: string): number {
  return options.value.findIndex(o => o.workspace === workspace)
}

function create(workspace: string) {
  picker.value?.resolve(workspace)
}

function cancel() {
  picker.value?.resolve(null)
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
    create(selected.value)
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    const list = options.value
    if (!list.length) return
    const dir = event.key === 'ArrowDown' ? 1 : -1
    const current = list.findIndex(o => o.workspace === selected.value)
    const next = (current + dir + list.length) % list.length
    selected.value = list[next].workspace
    nextTick(() => {
      const btn = optionButtons.value[next]
      btn?.focus()
    })
    return
  }
  if (/^[1-9]$/.test(event.key)) {
    const option = options.value[Number(event.key) - 1]
    if (option) {
      event.preventDefault()
      create(option.workspace)
    }
  }
}

watch(options, () => {
  const current = options.value.find(o => o.workspace === selected.value)
  if (!current) selected.value = options.value[0]?.workspace ?? ''
})

watch(picker, async value => {
  if (!value) return
  selected.value = options.value[0]?.workspace ?? ''
  await nextTick()
  optionButtons.value[0]?.focus()
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
.newchat-key {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg3);
  font-size: 11px;
  line-height: 1;
  font-weight: 700;
  flex: 0 0 auto;
}
.newchat-key--empty {
  border-color: transparent;
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
