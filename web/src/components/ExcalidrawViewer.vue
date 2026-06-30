<template>
  <div class="excalidraw-viewer">
    <div v-if="parseError" class="excalidraw-error">{{ parseError }}</div>
    <div v-else ref="hostEl" class="excalidraw-host"></div>
    <div v-if="!readOnly && saveState !== 'idle'" class="save-status" :class="saveState">
      <span class="status-dot"></span>
      <span class="status-text">{{ saveStatusText }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import React from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { Excalidraw } from '@excalidraw/excalidraw'
import '@excalidraw/excalidraw/index.css'

const props = withDefaults(
  defineProps<{
    content: string
    name: string
    filePath?: string
    chatId?: string
    readOnly?: boolean
  }>(),
  {
    readOnly: true,
  }
)

const emit = defineEmits<{
  (e: 'change', value: string): void
}>()

const hostEl = ref<HTMLElement>()
let root: Root | null = null

const sceneKey = ref(0)
const lastEmittedContent = ref('')
const saveState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle')
const saveStatusText = ref('')

let debounceTimer: ReturnType<typeof setTimeout> | null = null
let fadeTimer: ReturnType<typeof setTimeout> | null = null

const sceneResult = computed(() => {
  try {
    const parsed = JSON.parse(props.content || '{}')
    return {
      error: '',
      data: {
        elements: Array.isArray(parsed.elements) ? parsed.elements : [],
        appState: {
          ...(parsed.appState && typeof parsed.appState === 'object' ? parsed.appState : {}),
          viewModeEnabled: props.readOnly,
          zenModeEnabled: props.readOnly,
        },
        files: parsed.files && typeof parsed.files === 'object' ? parsed.files : undefined,
        scrollToContent: true,
      },
    }
  } catch {
    return {
      error: 'Invalid Excalidraw JSON.',
      data: null,
    }
  }
})
const parseError = computed(() => sceneResult.value.error)

function renderScene(): void {
  const host = hostEl.value
  const data = sceneResult.value.data
  if (!host || !data) {
    root?.unmount()
    root = null
    return
  }
  if (!root) root = createRoot(host)
  root.render(
    React.createElement(Excalidraw, {
      key: props.name + '_' + sceneKey.value,
      initialData: data,
      name: props.name,
      viewModeEnabled: props.readOnly,
      zenModeEnabled: props.readOnly,
      theme: 'dark',
      autoFocus: !props.readOnly,
      detectScroll: !props.readOnly,
      handleKeyboardGlobally: !props.readOnly,
      onChange: (elements, appState, files) => {
        if (props.readOnly) return

        try {
          const serialized = JSON.stringify({
            type: 'excalidraw',
            version: 2,
            source: 'https://excalidraw.com',
            elements: elements.filter(el => !el.isDeleted),
            appState: {
              viewBackgroundColor: appState.viewBackgroundColor,
              gridSize: appState.gridSize,
            },
            files: files,
          }, null, 2)

          if (serialized === lastEmittedContent.value) return

          lastEmittedContent.value = serialized
          emit('change', serialized)

          // Trigger debounced auto-save
          saveState.value = 'saving'
          saveStatusText.value = 'Saving...'
          if (debounceTimer) clearTimeout(debounceTimer)
          debounceTimer = setTimeout(() => {
            saveToFile(serialized)
          }, 1500)
        } catch (e) {
          console.error('Failed to serialize Excalidraw change:', e)
        }
      },
      UIOptions: {
        canvasActions: {
          changeViewBackgroundColor: !props.readOnly,
          clearCanvas: !props.readOnly,
          export: false,
          loadScene: false,
          saveAsImage: false,
          saveToActiveFile: false,
          toggleTheme: false,
        },
        tools: {
          image: false,
        },
      },
    }),
  )
}

async function saveToFile(serialized: string): Promise<void> {
  if (!props.filePath) {
    saveState.value = 'saved'
    saveStatusText.value = 'Changed'
    startFadeTimer()
    return
  }

  try {
    const response = await fetch('/api/workspace-file', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        path: props.filePath,
        content: serialized,
        chat_id: props.chatId || '',
      }),
    })

    if (response.ok) {
      saveState.value = 'saved'
      saveStatusText.value = 'Saved'
      startFadeTimer()
    } else {
      saveState.value = 'error'
      saveStatusText.value = 'Error saving'
    }
  } catch (e) {
    console.error('Failed to auto-save Excalidraw file:', e)
    saveState.value = 'error'
    saveStatusText.value = 'Error saving'
  }
}

function startFadeTimer() {
  if (fadeTimer) clearTimeout(fadeTimer)
  fadeTimer = setTimeout(() => {
    saveState.value = 'idle'
  }, 2000)
}

function cleanupTimers() {
  if (debounceTimer) clearTimeout(debounceTimer)
  if (fadeTimer) clearTimeout(fadeTimer)
}

watch(
  () => [props.content, props.name, props.readOnly],
  (newValues, oldValues) => {
    const [newContent, newName, newReadOnly] = newValues
    const [oldContent, oldName, oldReadOnly] = oldValues || [undefined, undefined, undefined]
    // Only re-render if file name/path/readOnly changed or external content update occurred
    if (
      newName !== oldName ||
      newReadOnly !== oldReadOnly ||
      (newContent && newContent !== lastEmittedContent.value)
    ) {
      sceneKey.value++
      nextTick(renderScene)
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  cleanupTimers()
  root?.unmount()
  root = null
})
</script>

<style scoped>
.excalidraw-viewer {
  position: relative;
  width: 100%;
  height: min(72vh, 760px);
  min-height: 420px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  background: #121212;
}

.excalidraw-host {
  width: 100%;
  height: 100%;
}

.excalidraw-error {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 220px;
  color: var(--error, #f87171);
  font-size: 13px;
}

/* Floating Save Status Pill */
.save-status {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 500;
  background: rgba(18, 18, 18, 0.85);
  border: 1px solid var(--border);
  backdrop-filter: blur(8px);
  color: var(--fg2, #cbd5e1);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  pointer-events: none;
  transition: all 0.2s ease;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fg3, #64748b);
  display: inline-block;
}

.save-status.saving .status-dot {
  background: var(--primary, #3b82f6);
  animation: pulse 1.2s infinite;
}

.save-status.saved .status-dot {
  background: var(--success, #10b981);
}

.save-status.error .status-dot {
  background: var(--error, #ef4444);
}

@keyframes pulse {
  0% { transform: scale(0.85); opacity: 0.5; }
  50% { transform: scale(1.2); opacity: 1; }
  100% { transform: scale(0.85); opacity: 0.5; }
}
</style>
