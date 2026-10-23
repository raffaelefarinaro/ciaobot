<script setup lang="ts">
/**
 * The chat's and the project's way into the browser terminal.
 *
 * This is the in-app terminal, not a native window: it opens the pane under
 * the chat, and it works from any browser that can reach the engine. The
 * signed-in session is the terminal's trust boundary — there is no password
 * to ask for — and the grant lives in an HttpOnly cookie. The native host
 * terminal keeps its own button (`HostTerminalButton.vue`) for the actions
 * that need a real window on the engine's own machine: provider sign-in,
 * skill updates, the engine's own update.
 */
import { useTerminalStore } from '../stores/terminal'

const props = defineProps<{
  chatId?: string
  projectId?: string
  /** `row`: a full-width rail row with the prompt glyph, for Chat details. */
  variant?: 'link' | 'row'
}>()

const store = useTerminalStore()

async function open(): Promise<void> {
  if (props.chatId) {
    await store.open({ chatId: props.chatId })
    return
  }
  if (props.projectId) {
    await store.open({ projectId: props.projectId })
  }
}
</script>

<template>
  <button v-if="variant === 'row'" type="button" class="terminal-row" @click="open">
    <span class="terminal-row-glyph" aria-hidden="true">›_</span>
    <span>Open terminal</span>
    <span class="terminal-row-arrow" aria-hidden="true">→</span>
  </button>
  <button v-else type="button" class="terminal-button" @click="open">Open in Terminal</button>
</template>

<style scoped>
.terminal-button {
  /* The same secondary text action the native host terminal used, so the
     rail does not change shape when the terminal moved into the app. */
  display: inline;
  padding: 0;
  border: 0;
  background: none;
  color: var(--fg);
  font: inherit;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 2px;
  cursor: pointer;
}
.terminal-button:hover { text-decoration-color: currentColor; }
.terminal-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }
/* One bordered row that is its own label, so the rail does not say
   "Terminal" twice. The glyph is the prompt mark background runs use. */
.terminal-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: 36px;
  padding: 0 var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
}
.terminal-row:hover { border-color: var(--border-strong); background: var(--bg3); }
.terminal-row:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.terminal-row-glyph { color: var(--fg3); font-family: var(--font-mono); font-size: var(--text-xs); font-weight: 600; }
.terminal-row-arrow { margin-left: auto; color: var(--fg3); }
.terminal-row:hover .terminal-row-arrow { color: var(--fg); }
@media (pointer: coarse) {
  .terminal-button { min-height: var(--touch); min-width: var(--touch); }
  .terminal-row { min-height: var(--touch); }
}
</style>
