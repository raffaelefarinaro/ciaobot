<template>
  <div
    v-if="request"
    class="confirm-backdrop"
    role="dialog"
    aria-modal="true"
    :aria-label="request.title"
    @click.self="cancel"
  >
    <div class="confirm-card">
      <p class="confirm-title">{{ request.title }}</p>
      <p class="confirm-message">{{ request.message }}</p>
      <div class="confirm-actions">
        <button
          ref="cancelButton"
          class="confirm-action confirm-action--cancel"
          type="button"
          @click="cancel"
        >
          {{ request.cancelLabel }}
        </button>
        <button
          class="confirm-action"
          :class="request.destructive ? 'confirm-action--danger' : 'confirm-action--primary'"
          type="button"
          @click="accept"
        >
          {{ request.confirmLabel }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { pendingConfirm } from '../lib/confirm'

const request = pendingConfirm
const cancelButton = ref<HTMLButtonElement | null>(null)

function accept() {
  request.value?.resolve(true)
}

function cancel() {
  request.value?.resolve(false)
}

// Escape must cancel, and Enter must confirm, so the dialog is usable without
// a mouse the way the native one was.
function onKeyDown(event: KeyboardEvent) {
  if (!request.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    cancel()
  } else if (event.key === 'Enter') {
    event.preventDefault()
    accept()
  }
}

// Focus the non-destructive action so a stray Return never confirms a delete.
watch(request, async value => {
  if (!value) return
  await nextTick()
  cancelButton.value?.focus()
})

onMounted(() => window.addEventListener('keydown', onKeyDown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown)
  // A dialog torn down mid-question would leave its caller awaiting forever.
  request.value?.resolve(false)
})
</script>

<style scoped>
.confirm-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgb(0 0 0 / 55%);
}
.confirm-card {
  width: 100%;
  max-width: 26rem;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--fg);
  box-shadow: 0 1.5rem 3rem rgb(0 0 0 / 45%);
}
.confirm-title {
  margin: 0 0 var(--space-2);
  font-weight: 600;
}
.confirm-message {
  margin: 0 0 var(--space-4);
  color: var(--fg2);
  white-space: pre-line;
}
.confirm-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-2);
}
.confirm-action {
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
.confirm-action:active {
  transform: scale(0.98);
}
.confirm-action--cancel {
  border-color: var(--border-strong);
  background: var(--bg3);
}
.confirm-action--cancel:hover {
  background: var(--border-strong);
}
.confirm-action--primary {
  border-color: var(--accent);
  background: var(--accent);
  color: white;
}
.confirm-action--primary:hover {
  border-color: var(--accent-strong);
  background: var(--accent-strong);
}
.confirm-action--danger {
  border-color: var(--error);
  background: var(--error);
  color: white;
}
.confirm-action--danger:hover {
  border-color: color-mix(in srgb, var(--error) 82%, black);
  background: color-mix(in srgb, var(--error) 82%, black);
}
</style>
