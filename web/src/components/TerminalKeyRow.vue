<script setup lang="ts">
/**
 * The on-screen keys a soft keyboard cannot produce (#1894).
 *
 * The row only renders buttons and reports presses; the pane owns the wire
 * and the screen. A tap must not take the focus from the shell — on a
 * phone that would also close the soft keyboard — so both pointerdown and
 * mousedown have their default prevented; the click still fires.
 * `pointer` is false for a keyboard activation (Enter/Space on a focused
 * key, `detail === 0`), which keeps the focus on the key.
 */
import { TERMINAL_KEYS, type TerminalKey } from '../lib/terminalKeys'

defineProps<{ disabled: boolean }>()
const emit = defineEmits<{ press: [key: TerminalKey, pointer: boolean] }>()

function onClick(key: TerminalKey, event: MouseEvent): void {
  emit('press', key, event.detail > 0)
}
</script>

<template>
  <div class="tp-keys" role="toolbar" aria-label="Terminal keys">
    <button
      v-for="def in TERMINAL_KEYS"
      :key="def.key"
      type="button"
      class="tp-key"
      :aria-label="def.name"
      :title="def.name"
      :disabled="disabled"
      @pointerdown.prevent
      @mousedown.prevent
      @click="onClick(def.key, $event)"
    >{{ def.label }}</button>
  </div>
</template>

<style scoped>
/* Hidden until a coarse pointer: a mouse and a keyboard do not need it.
   Under the screen, full width, scrolling sideways rather than wrapping. */
.tp-keys {
  display: none;
  flex: 0 0 auto;
  gap: var(--space-1);
  overflow-x: auto;
  padding-top: var(--space-2);
}
.tp-key {
  flex: 0 0 auto;
  min-width: var(--touch);
  min-height: var(--touch);
  padding: 0 var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg3);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  cursor: pointer;
}
.tp-key:disabled { opacity: 0.5; cursor: default; }
.tp-key:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
@media (pointer: coarse) {
  .tp-keys { display: flex; }
}
</style>
