<template>
  <DialogRoot
    :open="request !== null"
    modal
    @update:open="onOpenChange"
  >
    <DialogOverlay as-child>
      <div class="confirm-backdrop" @click.self="cancel">
        <DialogContent
          class="confirm-card"
          aria-modal="true"
          @open-auto-focus="onOpenAutoFocus"
          @escape-key-down="onEscapeKeyDown"
        >
          <DialogTitle as="p" class="confirm-title">
            {{ request?.title }}
          </DialogTitle>
          <DialogDescription as="p" class="confirm-message">
            {{ request?.message }}
          </DialogDescription>
          <div class="confirm-actions">
            <DialogClose as-child>
              <button
                ref="cancelButton"
                class="confirm-action confirm-action--cancel"
                type="button"
                @click="cancel"
              >
                {{ request?.cancelLabel }}
              </button>
            </DialogClose>
            <button
              class="confirm-action"
              :class="request?.destructive ? 'confirm-action--danger' : 'confirm-action--primary'"
              type="button"
              @click="accept"
            >
              {{ request?.confirmLabel }}
            </button>
          </div>
        </DialogContent>
      </div>
    </DialogOverlay>
  </DialogRoot>
</template>

<script setup lang="ts">
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'
import { onKeyStroke } from '@vueuse/core'
import { nextTick, onBeforeUnmount, ref } from 'vue'
import { pendingConfirm } from '../lib/confirm'

const request = pendingConfirm
const cancelButton = ref<HTMLButtonElement | null>(null)

function accept() {
  request.value?.resolve(true)
}

function cancel() {
  request.value?.resolve(false)
}

function onOpenChange(open: boolean) {
  if (!open) cancel()
}

function onOpenAutoFocus(event: Event) {
  event.preventDefault()
  nextTick(() => {
    if (request.value) cancelButton.value?.focus()
  })
}

function onEscapeKeyDown(event: KeyboardEvent) {
  event.preventDefault()
  cancel()
}

function onEnter(event: KeyboardEvent) {
  if (!request.value) return
  event.preventDefault()
  accept()
}

onKeyStroke('Enter', onEnter)

onBeforeUnmount(() => {
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
  color: var(--on-accent);
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
