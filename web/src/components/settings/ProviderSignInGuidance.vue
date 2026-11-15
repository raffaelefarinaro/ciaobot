<template>
  <div class="guidance">
    <p class="hint" v-if="cli === 'opencode'">Run this command yourself on the engine's computer and choose the provider in OpenCode. The command does not preselect one. OpenCode must be installed and available on your PATH.</p>
    <p class="hint" v-else>Run this command yourself on the engine's computer. Claude must be installed and available on your PATH.</p>
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
import { computed, ref, onUnmounted, watch } from 'vue'
import { writeClipboard } from '../../lib/codeCopy'
import { useTerminalStore } from '../../stores/terminal'

const props = defineProps<{ workspace: string, context: string, cli?: 'opencode' | 'claude' }>()
const command = computed(() => props.cli === 'claude' ? 'claude auth login' : 'opencode auth login')
const terminal = useTerminalStore()
const message = ref('')
let alive = true
let generation = 0
watch(() => props.workspace, () => { generation++; message.value = '' }, { flush: 'sync' })
onUnmounted(() => { alive = false })

async function copyCommand() {
  try {
    const copied = await writeClipboard(command.value)
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
