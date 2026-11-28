<script setup lang="ts">
import { ref } from 'vue'
import { useRunInTerminal } from '../composables/useRunInTerminal'
import type { ProviderConnection } from '../lib/types'

// The launch command and the follow-up are the provider row's `mcp_command` and
// `mcp_slash`. The browser never builds either. Settings → MCP is the only
// place it appears.
const props = defineProps<{
  connection: ProviderConnection
}>()

const terminal = useRunInTerminal()
const runNote = ref('')

async function openInTerminal() {
  const cmd = props.connection.mcp_command
  if (!cmd || terminal.busy.value) return
  const result = await terminal.run(cmd, { followUp: props.connection.mcp_slash })
  runNote.value = result === 'typed'
    ? ''
    : result === 'copied'
      ? 'The terminal was not ready, so the command was copied. Paste it into a terminal on the computer running Ciaobot.'
      : 'Could not reach the terminal or the clipboard. Run the command in a terminal on the computer running Ciaobot.'
}
</script>

<template>
  <div v-if="connection.mcp_command" class="mcp-open">
    <button class="btn-small" type="button" :disabled="terminal.busy.value" @click="openInTerminal">
      Open MCP settings in {{ connection.label }}
    </button>
    <p v-if="runNote" class="mcp-open-note" role="status">{{ runNote }}</p>
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
