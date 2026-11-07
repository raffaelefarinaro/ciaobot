<script setup lang="ts">
/**
 * Shows an engine helper terminal (a sign-in CLI the engine started for this
 * chat) in the chat's terminal pane. It never starts a shell and never types.
 */
import { useTerminalStore } from '../../stores/terminal'

const props = defineProps<{ chatId: string; sessionId: string }>()
const terminal = useTerminalStore()

async function show(): Promise<void> {
  if (!props.sessionId) return
  await terminal.open({ chatId: props.chatId }, { ensureSession: false })
  terminal.selectSession(props.sessionId)
}
</script>

<template>
  <button v-if="sessionId" type="button" class="needs-you-btn" @click="show">Show sign-in terminal</button>
</template>
