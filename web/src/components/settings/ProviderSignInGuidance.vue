<template>
  <div class="guidance">
    <p class="hint">{{ note || DEFAULT_NOTE }}</p>
    <code>{{ command }}</code>
    <div class="guidance-actions">
      <button type="button" class="set-link" :aria-label="`Copy sign-in command for ${context}`" @click="copyCommand">Copy</button>
      <button type="button" class="set-link" :aria-label="`Open browser terminal for ${context}`" @click="openTerminal">Open browser terminal</button>
    </div>
    <p class="hint">The terminal runs on the engine host. Workspace grouping is not isolation; shell startup files may run. Remote access is subject to terminal policy.</p>
    <p v-if="message" class="hint" role="status">{{ message }}</p>
  </div>
</template>

<script setup lang="ts">
import { ref, onUnmounted, watch } from 'vue'
import { writeClipboard } from '../../lib/codeCopy'
import { useTerminalStore } from '../../stores/terminal'

// The sign-in command is the connection payload's `command`, never a literal: each
// provider reports its own, so a new runtime cannot be shown another one's command.
const props = defineProps<{ workspace: string, context: string, command: string, note?: string }>()
const DEFAULT_NOTE = "Run this command yourself on the engine's computer. The CLI must be installed and available on your PATH."
const terminal = useTerminalStore()
const message = ref('')
let alive = true
let generation = 0
watch(() => props.workspace, () => { generation++; message.value = '' }, { flush: 'sync' })
onUnmounted(() => { alive = false })

async function copyCommand() {
  try {
    const copied = await writeClipboard(props.command)
    if (alive) message.value = copied ? 'Command copied.' : 'Could not copy. Select the command and copy it yourself.'
  } catch {
    if (alive) message.value = 'Could not copy. Select the command and copy it yourself.'
  }
}

async function openTerminal() {
  const current = generation
  if (!props.workspace) {
    message.value = 'Select a workspace before opening the terminal.'
    return
  }
  try {
    const result = await terminal.openWorkspaceForSettings(props.workspace)
    if (!alive || current !== generation) return
    message.value = result === 'busy'
      ? 'Another terminal is open. Close it before opening this workspace terminal.'
      : result === 'invalid'
        ? 'The workspace is unavailable. Select a workspace and try again.'
        : 'Terminal opening requested. Check the terminal pane for availability.'
  } catch {
    if (alive && current === generation) message.value = 'Could not open the terminal. You can run the command in a shell on the engine host.'
  }
}
</script>

<style scoped src="./settingsPanels.css"></style>
<style scoped>
.guidance { padding: var(--space-3) 0; min-width: 0; }
.guidance-actions { display: flex; flex-wrap: wrap; gap: var(--space-3); }
code { user-select: text; overflow-wrap: anywhere; }
@media (pointer: coarse) { button { min-height: 44px; min-width: 44px; } }
</style>
