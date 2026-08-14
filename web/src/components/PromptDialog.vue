<template>
  <div
    v-if="request"
    class="prompt-backdrop"
    role="dialog"
    aria-modal="true"
    :aria-label="request.title"
    @click.self="cancel"
  >
    <form class="prompt-card" @submit.prevent="accept">
      <p class="prompt-title">{{ request.title }}</p>
      <label class="prompt-label" for="prompt-dialog-input">{{ request.message }}</label>
      <input
        id="prompt-dialog-input"
        ref="input"
        v-model="draft"
        class="prompt-input"
        type="text"
        autocomplete="off"
        :placeholder="request.placeholder"
        @keydown.esc.prevent.stop="cancel"
      />
      <div class="prompt-actions">
        <button class="prompt-action prompt-action--cancel" type="button" @click="cancel">
          {{ request.cancelLabel }}
        </button>
        <button class="prompt-action prompt-action--primary" type="submit" :disabled="!draft.trim()">
          {{ request.confirmLabel }}
        </button>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { pendingPrompt } from '../lib/prompt'

const request = pendingPrompt
const input = ref<HTMLInputElement | null>(null)
const draft = ref('')

function accept() {
  const value = draft.value.trim()
  // Submitting nothing would resolve as "confirmed with an empty name", which
  // every caller has to special-case. An empty field simply is not a submit.
  if (!value) return
  request.value?.resolve(value)
}

function cancel() {
  request.value?.resolve(null)
}

// Esc is handled on the input rather than on window: ChatLayout's global
// Escape handler yields while a dialog is up, and keeping the key local means
// the press cannot also reach the view behind this one. Enter is the form's
// native submit.
watch(request, async value => {
  if (!value) return
  draft.value = value.value
  await nextTick()
  input.value?.focus()
  input.value?.select()
})

onBeforeUnmount(() => {
  // A dialog torn down mid-question would leave its caller awaiting forever.
  request.value?.resolve(null)
})
</script>

<style scoped>
.prompt-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgb(0 0 0 / 55%);
}
.prompt-card {
  width: 100%;
  max-width: 26rem;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--fg);
  box-shadow: 0 1.5rem 3rem rgb(0 0 0 / 45%);
}
.prompt-title {
  margin: 0 0 var(--space-2);
  font-weight: 600;
}
.prompt-label {
  display: block;
  margin: 0 0 var(--space-1);
  color: var(--fg2);
}
.prompt-input {
  width: 100%;
  min-height: 2.75rem;
  margin: 0 0 var(--space-4);
  padding: 0 var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  font: inherit;
}
.prompt-input:focus-visible {
  border-color: var(--accent);
  outline: none;
}
.prompt-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-2);
}
.prompt-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 6rem;
  min-height: 2.75rem;
  padding: 0 var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
  transition:
    background 120ms var(--ease),
    border-color 120ms var(--ease),
    color 120ms var(--ease),
    transform 120ms var(--ease);
}
.prompt-action:active {
  transform: scale(0.98);
}
.prompt-action--cancel {
  border-color: var(--border-strong);
  background: var(--bg3);
}
.prompt-action--cancel:hover {
  background: var(--border-strong);
}
.prompt-action--primary {
  border-color: var(--accent);
  background: var(--accent);
  color: white;
}
.prompt-action--primary:hover {
  border-color: var(--accent-strong);
  background: var(--accent-strong);
}
.prompt-action--primary:disabled {
  border-color: var(--border-strong);
  background: var(--bg3);
  color: var(--fg2);
  cursor: default;
}
</style>
