<template>
  <Teleport to="body">
    <div
      v-if="anchor"
      ref="rootEl"
      class="compose"
      :style="{ top: placed.top + 'px', left: placed.left + 'px' }"
      @mousedown.stop
    >
      <textarea
        ref="inputEl"
        :value="modelValue"
        class="compose-input"
        placeholder="Add a comment…"
        rows="3"
        @input="onInput"
        @keydown="onKeydown"
      ></textarea>
      <div v-if="images.length" class="compose-images">
        <span v-for="(img, i) in images" :key="img" class="compose-image">
          <img :src="`/api/images/${img}`" :alt="img" class="compose-thumb" />
          <button
            class="compose-image-remove"
            @click="emit('removeImage', i)"
            title="Remove"
            type="button"
          >&times;</button>
        </span>
      </div>
      <div class="compose-actions">
        <label class="compose-attach" title="Upload images">
          <input type="file" accept="image/*" multiple hidden @change="onUpload" />
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
        </label>
        <div class="compose-voice">
          <VoiceRecorder
            v-if="!transcribing"
            ref="voiceRecorderRef"
            @recorded="handleVoice"
            @error="handleVoiceError"
          />
          <span v-else class="voice-transcribing" title="Transcribing...">
            <span class="transcribe-spinner"></span>
          </span>
        </div>
        <button class="compose-btn" @click="emit('cancel')" type="button">Cancel</button>
        <button
          class="compose-btn primary"
          :disabled="!modelValue.trim()"
          @click="emit('save')"
          type="button"
        >Add comment</button>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
// Shared floating compose popover for selection comments.
// Used by ChatPanel, FileViewerModal, and PinnedFilePanel so the UI stays
// identical. Anchor is viewport-fixed (Teleport to body). The selection quote
// is intentionally omitted — the highlight already shows what was selected.
//
// Callers pass the raw point they want the popover near; keeping it on screen is
// this component's job, because only it knows how tall the box actually is (the
// textarea and an image row make that vary). Callers used to each guess a
// reserve height and they disagreed, so a popover opened near the bottom edge
// could put its Save button past the fold, where `position: fixed` means no
// amount of scrolling reaches it.
import { computed, nextTick, ref, watch } from 'vue'

import { useViewportHeight } from '../composables/useViewportHeight'
import { clampAnchorLeft, clampAnchorTop } from '../lib/popoverAnchor'
import { useProjectStore } from '../stores/projects'
import { errorMessage } from '../lib/errorMessage'
import VoiceRecorder from './VoiceRecorder.vue'

type ComposeAnchor = { top: number; left: number }

// Pre-measurement fallback: the width is fixed in this component's CSS, and the
// height covers a 3-row textarea plus the action row.
const COMPOSE_W = 280
const COMPOSE_H = 208

const props = withDefaults(defineProps<{
  anchor: ComposeAnchor | null
  modelValue: string
  images?: string[]
}>(), {
  images: () => [],
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  cancel: []
  save: []
  upload: [event: Event]
  removeImage: [index: number]
}>()

const images = computed(() => props.images ?? [])
const inputEl = ref<HTMLTextAreaElement>()
const rootEl = ref<HTMLElement>()
const voiceRecorderRef = ref<InstanceType<typeof VoiceRecorder> | null>(null)
const transcribing = ref(false)
// Measured height, once rendered. Null until then, so the first paint uses the
// COMPOSE_H estimate rather than jumping.
const measuredH = ref<number | null>(null)
const store = useProjectStore()

// Reactive on purpose. Opening this popover focuses the textarea, so on a phone
// the keyboard comes up a moment later and shrinks the viewport under a box that
// has already been placed. Without re-clamping, a popover anchored low sits
// behind the keyboard with no way to reach it, since `position: fixed` means
// scrolling does nothing.
const viewportH = useViewportHeight()

const placed = computed<ComposeAnchor>(() => {
  const a = props.anchor ?? { top: 0, left: 0 }
  return {
    top: clampAnchorTop(a.top, measuredH.value ?? COMPOSE_H, viewportH.value),
    left: clampAnchorLeft(a.left, COMPOSE_W),
  }
})

function measure(): void {
  nextTick(() => {
    const h = rootEl.value?.offsetHeight
    if (h) measuredH.value = h
  })
}

// Re-measure whenever the box can change size or move: a new anchor means a new
// open, and an image row grows it past the estimate.
watch(() => props.anchor, (a) => { if (a) measure() })
watch(images, () => { if (props.anchor) measure() })

function onInput(e: Event): void {
  emit('update:modelValue', (e.target as HTMLTextAreaElement).value)
}

function onUpload(e: Event): void {
  emit('upload', e)
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('cancel')
    return
  }
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault()
    emit('save')
    return
  }
  // Same dictation shortcut that opens this popover from a selection, so it
  // keeps working once the textarea has focus.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 'd' || e.key === 'D')) {
    e.preventDefault()
    toggleDictation()
  }
}

function focus(): void {
  nextTick(() => {
    const el = inputEl.value
    if (!el) return
    el.focus()
    // Type-to-comment seeds the box with the first keystroke or a paste, and a
    // freshly focused textarea would otherwise put the caret before it.
    const end = el.value.length
    el.setSelectionRange(end, end)
  })
}

watch(
  () => props.anchor,
  (a) => {
    if (a) focus()
  },
)

function insertTextAtCursor(text: string): void {
  const el = inputEl.value
  if (!el) return
  const start = el.selectionStart ?? 0
  const end = el.selectionEnd ?? start
  const before = props.modelValue.slice(0, start)
  const after = props.modelValue.slice(end)
  const next = before + text + after
  emit('update:modelValue', next)
  nextTick(() => {
    el.focus()
    const pos = start + text.length
    el.setSelectionRange(pos, pos)
  })
}

async function handleVoice(blob: Blob): Promise<void> {
  const chatId = store.activeChatId
  if (!chatId) {
    store.pushErrorToast('Voice dictation unavailable', 'No active chat')
    return
  }
  transcribing.value = true
  try {
    const text = await store.transcribeVoice(chatId, blob)
    if (text.trim()) {
      insertTextAtCursor(text.trimEnd())
    }
  } catch (e) {
    console.error('Voice error:', e)
    store.pushErrorToast('Voice transcription failed', `${errorMessage(e)}`)
  } finally {
    transcribing.value = false
  }
}

function handleVoiceError(message: string): void {
  store.pushErrorToast('Voice dictation unavailable', message)
}

// Allow the parent (and a future global shortcut) to toggle recording.
function toggleDictation(): void {
  voiceRecorderRef.value?.toggleRecording()
}

defineExpose({ focus, toggleDictation })
</script>

<style scoped>
.compose {
  position: fixed;
  z-index: 41;
  width: 280px;
  max-width: calc(100vw - 16px);
  background: var(--bg);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent, #60a5fa);
  border-radius: 8px;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
  padding: 10px 12px;
  box-sizing: border-box;
}
.compose-input {
  width: 100%;
  resize: vertical;
  min-height: 60px;
  font-family: inherit;
  font-size: var(--text-base);
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  box-sizing: border-box;
}
.compose-input:focus {
  outline: none;
  border-color: var(--accent, #60a5fa);
}
.compose-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0;
}
.compose-image {
  position: relative;
  display: inline-flex;
}
.compose-thumb {
  height: 40px;
  width: 40px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.compose-image-remove {
  position: absolute;
  top: -4px;
  right: -4px;
  width: 16px;
  height: 16px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: var(--fg);
  color: var(--bg);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
}
.compose-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}
.compose-attach {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--fg2);
  margin-right: auto;
}
.compose-attach:hover {
  background: var(--bg3);
  color: var(--fg);
  border-color: var(--fg2);
}
.compose-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  cursor: pointer;
}
.compose-btn:hover {
  background: var(--bg2, rgba(255, 255, 255, 0.04));
}
.compose-btn.primary {
  background: var(--accent, #60a5fa);
  border-color: var(--accent, #60a5fa);
  color: var(--bg);
}
.compose-btn.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.compose-voice {
  display: flex;
  align-items: center;
}
.compose-voice :deep(.voice-btn) {
  min-width: 28px;
  min-height: 28px;
  width: 28px;
  height: 28px;
  border-radius: 4px;
}
.compose-voice :deep(.voice-btn svg) {
  width: 16px;
  height: 16px;
}
.voice-transcribing {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
}
.transcribe-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--accent, #60a5fa);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 640px) {
  .compose {
    left: 8px !important;
    right: 8px;
    width: auto;
    max-width: none;
  }
}
</style>
