<template>
  <div v-if="modelValue" class="modal-backdrop" @click.self="close">
    <div class="command-palette modal-sheet" role="dialog" aria-modal="true" aria-label="Command Palette">
      <div class="palette-header">
        <span class="wordmark wordmark--sm">cmd</span>
        <input
          ref="searchInput"
          v-model="query"
          type="text"
          class="palette-input"
          placeholder="Type a command or search chats..."
          @keydown.down.prevent="moveSelection(1)"
          @keydown.up.prevent="moveSelection(-1)"
          @keydown.enter.prevent="executeSelected"
          @keydown.esc="close"
        />
        <button class="btn-chip" @click="close">Esc</button>
      </div>

      <div class="palette-results">
        <div
          v-for="(item, index) in filteredItems"
          :key="item.id"
          class="palette-item"
          :class="{ active: index === selectedIndex }"
          @mouseenter="selectedIndex = index"
          @click="executeItem(item)"
        >
          <span class="item-icon">{{ item.icon }}</span>
          <div class="item-content">
            <span class="item-title">{{ item.title }}</span>
            <span v-if="item.subtitle" class="item-subtitle">{{ item.subtitle }}</span>
          </div>
          <span v-if="item.category" class="badge badge--muted">{{ item.category }}</span>
        </div>

        <div v-if="filteredItems.length === 0" class="palette-empty">
          No matching actions or chats found
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'

interface CommandItem {
  id: string
  title: string
  subtitle?: string
  category: string
  icon: string
  action: () => void
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', val: boolean): void }>()

const router = useRouter()
const projectStore = useProjectStore()

const query = ref('')
const selectedIndex = ref(0)
const searchInput = ref<HTMLInputElement | null>(null)

function close() {
  emit('update:modelValue', false)
  query.value = ''
  selectedIndex.value = 0
}

watch(() => props.modelValue, (isOpen) => {
  if (isOpen) {
    selectedIndex.value = 0
    query.value = ''
    nextTick(() => searchInput.value?.focus())
  }
})

const defaultCommands = computed<CommandItem[]>(() => [
  {
    id: 'cmd-new-chat',
    title: 'New Chat',
    subtitle: 'Create a new project chat',
    category: 'Actions',
    icon: '💬',
    action: () => {
      close()
      router.push('/')
    },
  },
  {
    id: 'cmd-schedules',
    title: 'Schedules & Routines',
    subtitle: 'View active recurring schedules and loops',
    category: 'Navigation',
    icon: '⏰',
    action: () => {
      close()
      router.push('/schedules')
    },
  },
  {
    id: 'cmd-settings',
    title: 'Settings',
    subtitle: 'Manage models, workspaces, and preferences',
    category: 'Navigation',
    icon: '⚙️',
    action: () => {
      close()
      router.push('/settings')
    },
  },
  {
    id: 'cmd-toggle-theme',
    title: 'Toggle Theme',
    subtitle: 'Switch between light and dark themes',
    category: 'Appearance',
    icon: '🌗',
    action: () => {
      // Persist to the same key Settings/main.ts use, else the toggle is lost
      // on reload and desyncs from the Settings theme selector.
      const nextTheme = document.documentElement.classList.contains('theme-light') ? 'dark' : 'light'
      localStorage.setItem('ciao-theme', nextTheme)
      document.documentElement.classList.toggle('theme-light', nextTheme === 'light')
      close()
    },
  },
])

const chatItems = computed<CommandItem[]>(() => {
  const projById = new Map(projectStore.projects.map((p) => [p.project_id, p]))
  return projectStore.chats.map((chat) => {
    const proj = projById.get(chat.project_id)
    return {
      id: `chat-${chat.chat_id}`,
      title: chat.title || 'Untitled chat',
      subtitle: proj ? proj.name : 'General',
      category: 'Chats',
      icon: '📄',
      action: () => {
        close()
        router.push(`/chat/${chat.chat_id}`)
      },
    }
  })
})

const allItems = computed(() => [...defaultCommands.value, ...chatItems.value])

const filteredItems = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return allItems.value.slice(0, 15)
  return allItems.value
    .filter(
      (item) =>
        item.title.toLowerCase().includes(q) ||
        (item.subtitle && item.subtitle.toLowerCase().includes(q)) ||
        item.category.toLowerCase().includes(q)
    )
    .slice(0, 15)
})

function moveSelection(delta: number) {
  if (filteredItems.value.length === 0) return
  const max = filteredItems.value.length
  selectedIndex.value = (selectedIndex.value + delta + max) % max
}

function executeSelected() {
  const item = filteredItems.value[selectedIndex.value]
  if (item) executeItem(item)
}

function executeItem(item: CommandItem) {
  item.action()
}
</script>

<style scoped>
.command-palette {
  max-width: 600px;
  background: var(--bg-elev);
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-strong);
  overflow: hidden;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
}

.palette-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
}

.palette-input {
  flex: 1;
  border: none;
  background: transparent;
  padding: 8px 0;
  font-size: var(--text-base);
  color: var(--fg);
  outline: none;
}

.palette-results {
  max-height: 380px;
  overflow-y: auto;
  padding: var(--space-2);
}

.palette-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background 120ms var(--ease);
}

.palette-item.active {
  background: var(--bg3);
}

.item-icon {
  font-size: 16px;
}

.item-content {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.item-title {
  font-size: var(--text-base);
  font-weight: 500;
  color: var(--fg);
}

.item-subtitle {
  font-size: var(--text-xs);
  color: var(--fg2);
}

.palette-empty {
  padding: var(--space-5);
  text-align: center;
  color: var(--fg2);
  font-size: var(--text-sm);
}
</style>
