<template>
  <Teleport to="body">
    <div
      v-if="anchor"
      class="compose"
      :style="{ top: anchor.top + 'px', left: anchor.left + 'px' }"
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
import { computed, nextTick, ref, watch } from 'vue'

export type ComposeAnchor = { top: number; left: number }

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
  }
}

function focus(): void {
  nextTick(() => inputEl.value?.focus())
}

watch(
  () => props.anchor,
  (a) => {
    if (a) focus()
  },
)

defineExpose({ focus })
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

@media (max-width: 640px) {
  .compose {
    left: 8px !important;
    right: 8px;
    width: auto;
    max-width: none;
  }
}
</style>
