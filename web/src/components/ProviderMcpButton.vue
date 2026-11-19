<script setup lang="ts">
import { ref } from 'vue'
import { useRunInTerminal } from '../composables/useRunInTerminal'
import type { ProviderConnection } from '../lib/types'

// The command is the provider row's `mcp_command`. The browser never builds it.
const props = defineProps<{
  connection: ProviderConnection
  chatId?: string
}>()

const terminal = useRunInTerminal()
const copied = ref(false)
const runNote = ref('')

const RELOAD_NOTE = 'Changes apply to new chats. Ask the agent to run `ciao chat reload` to load them here.'

async function openInTerminal() {
  const cmd = props.connection.mcp_command
  if (!cmd || terminal.busy.value) return
  const result = await terminal.run(cmd, props.chatId ? { chatId: props.chatId } : undefined)
  runNote.value = result === 'typed'
    ? "Running in this chat's terminal. Finish signing in there."
    : result === 'copied'
      ? 'The terminal was not ready, so the command was copied. Paste it into a terminal on the computer running Ciaobot.'
      : 'Could not reach the terminal or the clipboard. Select the command above and run it on the computer running Ciaobot.'
}

async function copy() {
  const cmd = props.connection.mcp_command
  if (!cmd || !navigator.clipboard) return
  try {
    await navigator.clipboard.writeText(cmd)
    copied.value = true
  } catch {
    // Clipboard access can be denied (no permission, no secure context).
  }
}
</script>

<template>
  <div v-if="connection.mcp_command" class="mcp-open">
    <button class="btn-small" type="button" :disabled="terminal.busy.value" @click="openInTerminal">
      Open MCP settings in {{ connection.label }}
    </button>
    <div class="mcp-open-cmd">
      <code>{{ connection.mcp_command }}</code>
      <button class="btn-small" type="button" @click="copy">{{ copied ? 'Copied' : 'Copy' }}</button>
    </div>
    <p class="mcp-open-note" role="status">{{ runNote || RELOAD_NOTE }}</p>
  </div>
</template>

<style scoped>
.mcp-open {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
.mcp-open-cmd {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.mcp-open-cmd code {
  user-select: all;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--bg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}
.mcp-open-note {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
}
.mcp-open button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
@media (pointer: coarse) {
  .mcp-open .btn-small { min-height: 44px; min-width: 44px; }
}
</style>
