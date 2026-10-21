<script setup lang="ts">
/**
 * The chat's and the project's way into the browser terminal.
 *
 * This is the in-app terminal, not a native window: it opens the pane under
 * the chat, and it works from any browser that can reach the engine — the
 * password is asked for once, and the grant lives in a cookie. The native
 * host terminal keeps its own button (`HostTerminalButton.vue`) for the
 * actions that need a real window on the engine's own machine: provider
 * sign-in, skill updates, the engine's own update.
 */
import { useTerminalStore } from '../stores/terminal'

const props = defineProps<{
  chatId?: string
  projectId?: string
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
  <button type="button" class="terminal-button" @click="open">Open in Terminal</button>
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
@media (pointer: coarse) {
  .terminal-button { min-height: var(--touch); min-width: var(--touch); }
}
</style>
