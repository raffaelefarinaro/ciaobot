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
 *
 * Opening from here starts a shell when the scope has none, so the pane is
 * never an empty box; a chat with a linked repository asks which folder.
 */
import { useTerminalStore } from '../stores/terminal'
import AppIcon from './AppIcon.vue'

const props = defineProps<{
  chatId?: string
  projectId?: string
  /** `row`: the Terminal row of Chat details' Open section. */
  variant?: 'link' | 'row'
}>()

const store = useTerminalStore()

async function open(): Promise<void> {
  if (props.chatId) {
    await store.open({ chatId: props.chatId }, { ensureSession: true })
    return
  }
  if (props.projectId) {
    await store.open({ projectId: props.projectId }, { ensureSession: true })
  }
}
</script>

<template>
  <button v-if="variant === 'row'" type="button" class="cd-row cd-row--link terminal-row" aria-label="Open terminal" @click="open">
    <AppIcon class="cd-icon" name="terminal" />
    <span class="cd-main">
      <span class="cd-primary">Terminal</span>
      <span class="cd-detail">Shell on the engine's computer</span>
    </span>
    <span class="cd-trail" aria-hidden="true"><AppIcon name="arrow" /></span>
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
@media (pointer: coarse) {
  .terminal-button { min-height: var(--touch); min-width: var(--touch); }
}
</style>
<style scoped src="./chatDetails.css"></style>
