<script setup lang="ts">
/**
 * A window that opens over the current view: the file viewer's dialog contract
 * as a shared shell. A modal Reka dialog, focus moved in on open and handed
 * back to the opener on close, Escape and a plain backdrop click dismissing it,
 * and a × in the title bar that does the same.
 *
 * The owner decides what closes it: the shell only emits `close`, and the
 * `open` prop is the owner's state. Slots fill the title bar's `actions` and the
 * scrolling `default` body.
 */
import { nextTick, ref, watch } from 'vue'
import {
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogRoot,
  DialogTitle,
  type PointerDownOutsideEvent,
} from 'reka-ui'

const props = defineProps<{
  open: boolean
  title: string
  subtitle?: string
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

// The element focused when the sheet opened, so closing can hand focus back.
const opener = ref<HTMLElement | null>(null)
watch(() => props.open, (open, wasOpen) => {
  if (open && !wasOpen && document.activeElement instanceof HTMLElement) {
    opener.value = document.activeElement
  }
}, { flush: 'sync', immediate: true })

function onOpenChange(open: boolean): void {
  if (!open) emit('close')
}

/**
 * Focus goes to the sheet, which is the focus scope's own container and so the
 * event's target. A template ref would not be reliably set by the time the
 * scope mounts.
 */
function onOpenAutoFocus(event: Event): void {
  event.preventDefault()
  const sheet = event.target instanceof HTMLElement ? event.target : null
  sheet?.focus()
  nextTick(() => sheet?.focus())
}

function onCloseAutoFocus(event: Event): void {
  event.preventDefault()
  nextTick(() => {
    if (opener.value?.isConnected) opener.value.focus()
  })
}

function onEscapeKeyDown(event: KeyboardEvent): void {
  event.preventDefault()
  emit('close')
}

function onPointerDownOutside(event: PointerDownOutsideEvent): void {
  event.preventDefault()
  const original = event.detail.originalEvent
  if (original.button !== 0 || original.ctrlKey) return
  emit('close')
}
</script>

<template>
  <DialogRoot :open="open" modal @update:open="onOpenChange">
    <DialogOverlay as-child>
      <div class="ov-backdrop">
        <DialogContent
          as-child
          aria-modal="true"
          @open-auto-focus="onOpenAutoFocus"
          @close-auto-focus="onCloseAutoFocus"
          @escape-key-down="onEscapeKeyDown"
          @pointer-down-outside="onPointerDownOutside"
        >
          <div class="ov-sheet" tabindex="-1">
            <header class="ov-header">
              <div class="ov-titles">
                <DialogTitle as="div" class="ov-title">{{ title }}</DialogTitle>
                <DialogDescription v-if="subtitle" as="div" class="ov-subtitle">{{ subtitle }}</DialogDescription>
              </div>
              <div class="ov-actions">
                <slot name="actions" />
                <button type="button" class="btn-icon" title="Close (Esc)" aria-label="Close" @click="emit('close')">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                </button>
              </div>
            </header>
            <div class="ov-body">
              <slot />
            </div>
          </div>
        </DialogContent>
      </div>
    </DialogOverlay>
  </DialogRoot>
</template>

<style scoped>
.ov-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 16px;
  outline: none;
}
.ov-sheet {
  /* The file viewer's window: the sidebar's surface, 14px radius, a 1px border. */
  background: var(--bg2);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: min(760px, 100%);
  max-height: min(88vh, 900px);
  display: flex;
  flex-direction: column;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
  overflow: hidden;
  position: relative;
  outline: none;
}
.ov-header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 52px;
  padding: 4px 8px 4px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  box-sizing: border-box;
}
.ov-titles {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ov-title {
  flex: 0 0 auto;
  max-width: 100%;
  font-weight: 650;
  font-size: var(--text-lg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ov-subtitle {
  flex: 0 1 auto;
  min-width: 0;
  font-size: var(--text-sm);
  color: var(--fg3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}
.ov-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
  align-items: center;
}
.ov-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 20px 24px 32px;
}
@media (max-width: 640px) {
  .ov-backdrop { padding: 0; }
  .ov-sheet {
    width: 100vw;
    max-height: calc(100dvh - var(--safe-top) - var(--safe-bottom));
    height: calc(100dvh - var(--safe-top) - var(--safe-bottom));
    margin-top: var(--safe-top);
    margin-bottom: var(--safe-bottom);
    border-radius: 0;
  }
  .ov-body { padding: 14px 16px 28px; }
}
@media (pointer: coarse) {
  .ov-actions > .btn-icon,
  .ov-actions :slotted(*) { min-width: var(--touch); min-height: var(--touch); }
}
</style>
