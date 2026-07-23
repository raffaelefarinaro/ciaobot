<template>
  <div
    v-if="modelValue"
    class="modal-backdrop"
    @click.self="close"
    @keydown.esc.prevent.stop="close"
  >
    <div
      ref="paletteEl"
      class="command-palette modal-sheet"
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      @keydown.tab="trapFocus"
    >
      <div class="palette-header">
        <span class="wordmark wordmark--sm">cmd</span>
        <input
          ref="searchInput"
          v-model="query"
          type="text"
          class="palette-input"
          placeholder="Type a command or search chats..."
          role="combobox"
          aria-autocomplete="list"
          aria-controls="command-palette-results"
          :aria-expanded="true"
          :aria-activedescendant="selectedOptionId"
          @keydown.down.prevent="moveSelection(1)"
          @keydown.up.prevent="moveSelection(-1)"
          @keydown.enter.prevent="executeSelected"
          @keydown.esc="close"
        />
        <button class="btn-chip" @click="close">Esc</button>
      </div>

      <div
        id="command-palette-results"
        ref="resultsEl"
        class="palette-results"
        role="listbox"
      >
        <button
          v-for="(item, index) in filteredItems"
          :key="item.id"
          :id="optionId(item)"
          type="button"
          role="option"
          :aria-selected="index === selectedIndex"
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
        </button>

        <div v-if="filteredItems.length === 0" class="palette-empty">
          No matching actions or chats found
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'

interface CommandItem {
  id: string
  title: string
  subtitle?: string
  category: string
  icon: string
  action: () => void | Promise<void>
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', val: boolean): void }>()

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

const query = ref('')
const selectedIndex = ref(0)
const searchInput = ref<HTMLInputElement | null>(null)
const paletteEl = ref<HTMLElement | null>(null)
const resultsEl = ref<HTMLElement | null>(null)
let previouslyFocused: HTMLElement | null = null

function close() {
  emit('update:modelValue', false)
  query.value = ''
  selectedIndex.value = 0
}

watch(() => props.modelValue, (isOpen) => {
  if (isOpen) {
    previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    selectedIndex.value = 0
    query.value = ''
    nextTick(() => searchInput.value?.focus())
  } else {
    nextTick(() => previouslyFocused?.focus())
  }
})

function newChatProjectId(): string | null {
  const routeProjectId = typeof route.params.projectId === 'string'
    ? route.params.projectId
    : ''
  if (routeProjectId && projectStore.projects.some(p => p.project_id === routeProjectId)) {
    return routeProjectId
  }
  if (projectStore.activeProject) {
    return projectStore.activeProject.project_id
  }
  return projectStore.projects.find(
    p => p.workspace === projectStore.activeWorkspace && p.is_auto && p.name === 'General',
  )?.project_id || null
}

const defaultCommands = computed<CommandItem[]>(() => [
  {
    id: 'cmd-new-chat',
    title: 'New Chat',
    subtitle: 'Create a new project chat',
    category: 'Actions',
    icon: '💬',
    action: async () => {
      const projectId = newChatProjectId()
      if (!projectId) {
        projectStore.pushErrorToast(
          'Could not create chat',
          'No project is available in the current workspace.',
        )
        return
      }
      try {
        const chat = await projectStore.createChat(projectId)
        close()
        await router.push(`/chat/${chat.chat_id}`)
      } catch (error) {
        projectStore.pushErrorToast(
          'Could not create chat',
          error instanceof Error ? error.message : String(error),
        )
      }
    },
  },
  {
    id: 'cmd-schedules',
    title: 'Automations',
    subtitle: 'View schedules, routines, and loops',
    category: 'Navigation',
    icon: '⏰',
    action: () => {
      close()
      void router.push('/schedules')
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
      void router.push('/settings')
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
        void router.push(`/chat/${chat.chat_id}`)
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

watch(
  () => filteredItems.value.length,
  (length) => {
    selectedIndex.value = length > 0
      ? Math.min(selectedIndex.value, length - 1)
      : 0
  },
)

function optionId(item: CommandItem): string {
  return `command-option-${item.id}`
}

const selectedOptionId = computed(() => {
  const item = filteredItems.value[selectedIndex.value]
  return item ? optionId(item) : undefined
})

watch(selectedOptionId, () => {
  nextTick(() => {
    const container = resultsEl.value
    const option = selectedOptionId.value
      ? document.getElementById(selectedOptionId.value)
      : null
    if (!container || !option) return
    const optionTop = option.offsetTop
    const optionBottom = optionTop + option.offsetHeight
    let nextTop = container.scrollTop
    if (optionTop < container.scrollTop) {
      nextTop = optionTop
    } else if (optionBottom > container.scrollTop + container.clientHeight) {
      nextTop = optionBottom - container.clientHeight
    }
    if (nextTop === container.scrollTop) return
    if (typeof container.scrollTo === 'function') {
      container.scrollTo({ top: nextTop, behavior: 'smooth' })
    } else {
      container.scrollTop = nextTop
    }
  })
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
  void item.action()
}

function trapFocus(event: KeyboardEvent): void {
  const focusable = paletteEl.value?.querySelectorAll<HTMLElement>(
    'input, button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
  )
  if (!focusable?.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
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
  min-width: 0;
  border: none;
  background: transparent;
  padding: 8px 0;
  font-size: var(--text-base);
  color: var(--fg);
  outline: none;
}

.palette-input:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

.palette-results {
  max-height: 380px;
  overflow-y: auto;
  padding: var(--space-2);
}

.palette-item {
  width: 100%;
  min-height: var(--touch);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 120ms var(--ease);
}

.palette-item.active {
  background: var(--bg3);
}

.palette-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.item-icon {
  font-size: 16px;
}

.item-content {
  flex: 1;
  min-width: 0;
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

@media (max-width: 720px) {
  .palette-input {
    font-size: 16px;
  }
}
</style>
