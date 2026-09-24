<template>
  <Teleport to="body">
    <FocusScope
      v-if="anchor"
      as-child
      loop
      :trapped="false"
      @mount-auto-focus="onMountAutoFocus"
    >
      <div
        class="compose"
        role="dialog"
        aria-label="Add comment"
        :style="{ top: placed.top + 'px', left: placed.left + 'px' }"
        @mousedown.stop
        @keydown="onKeydown"
      >
      <textarea
        ref="inputEl"
        :value="modelValue"
        class="compose-input"
        placeholder="Add a note for Ciao"
        rows="3"
        @input="onInput"
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
        <span class="compose-hint">Sent with your next message</span>
        <button class="compose-btn" @click="emit('cancel')" type="button">Cancel</button>
        <button
          class="compose-btn primary"
          :disabled="!modelValue.trim()"
          @click="emit('save')"
          type="button"
        >Add comment</button>
      </div>
      </div>
    </FocusScope>
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
import { FocusScope } from 'reka-ui'
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
    const h = inputEl.value?.closest<HTMLElement>('.compose')?.offsetHeight
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

function onMountAutoFocus(event: Event): void {
  event.preventDefault()
  focus()
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
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
    e.stopPropagation()
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
  { immediate: true },
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
  width: 320px;
  max-width: calc(100vw - 16px);
  box-sizing: border-box;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  background: var(--bg2);
  box-shadow: 0 14px 36px rgb(0 0 0 / 28%);
}
.compose-input {
  width: 100%;
  resize: vertical;
  min-height: 64px;
  box-sizing: border-box;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  color: var(--fg);
  font-family: inherit;
  font-size: var(--text-base);
  line-height: 1.45;
}
.compose-input:focus {
  outline: none;
  border-color: var(--accent);
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
  width: 30px;
  height: 30px;
  border-radius: 6px;
  color: var(--fg2);
}
.compose-attach:hover {
  background: var(--bg3);
  color: var(--fg);
}
.compose-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
  font-weight: 600;
}
.compose-btn:hover {
  border-color: var(--border-strong);
}
.compose-btn.primary {
  border-color: transparent;
  background: var(--accent);
  color: var(--on-accent);
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
  min-width: 30px;
  min-height: 30px;
  width: 30px;
  height: 30px;
  border-color: transparent;
  border-radius: 6px;
}
.compose-hint {
  margin-right: auto;
  min-width: 0;
  color: var(--fg3);
  font-size: var(--text-xs);
  line-height: 1.3;
}
.compose-attach { margin-right: 0; }
@media (pointer: coarse) {
  .compose-btn { min-height: var(--touch); }
  .compose-attach,
  .compose-voice :deep(.voice-btn) { width: var(--touch); height: var(--touch); min-width: var(--touch); min-height: var(--touch); }
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
@media (pointer: coarse) {
  .compose-attach,
  .compose-btn,
  .compose-voice :deep(.voice-btn),
  .voice-transcribing {
    min-width: var(--touch);
    min-height: var(--touch);
    width: var(--touch);
    height: var(--touch);
  }

  .compose-image-remove {
    box-sizing: content-box;
    top: -12px;
    right: -12px;
    padding: 14px;
  }
}
</style>
