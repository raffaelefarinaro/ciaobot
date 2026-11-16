<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { writeClipboard } from '../lib/codeCopy'
import { mcpCommand, validServerName } from '../lib/shellQuote'
import type { McpNativeServer, McpStatus } from '../lib/types'

const props = defineProps<{ chatId: string; provider: 'claude' | 'opencode'; server: McpNativeServer; status: McpStatus }>()
const busy = ref(false)
const notice = ref('')
let generation = 0
const context = computed(() => JSON.stringify([props.chatId, props.provider, props.server?.name, props.server?.scope, props.server?.enabled, props.server?.allowed, props.server?.kind, props.status?.workspace, props.status?.agent_root, props.status?.shell?.kind, props.status?.shell?.cd_prefix]))
const rows = computed(() => {
  const { server, status, provider } = props
  if (!server || !status?.shell || status.shell.kind !== 'posix' || typeof status.shell.cd_prefix !== 'string' || server.enabled === false || server.allowed === false || server.scope === 'claudeai' || !validServerName(server.name)) return []
  const actions = ['list', ...(['http', 'sse', 'remote'].includes(server.kind ?? '') ? ['login'] : [])] as ('list' | 'login')[]
  return actions.flatMap(action => {
    const command = mcpCommand({ provider, action, name: server.name, shell: status.shell.kind, cdPrefix: status.shell.cd_prefix })
    return command === null ? [] : [{ action, command, context: context.value }]
  })
})
watch(context, () => { generation++; notice.value = ''; busy.value = false }, { flush: 'sync' })
onBeforeUnmount(() => { generation++ })
async function copy(row: { action: 'list' | 'login'; command: string; context: string }) {
  if (busy.value || row.context !== context.value || !rows.value.some(current => current.action === row.action && current.command === row.command && current.context === row.context)) return
  busy.value = true
  notice.value = ''
  const ticket = generation
  let copied = false
  try { copied = await writeClipboard(row.command, () => ticket === generation && row.context === context.value) } catch { /* The preview remains available. */ }
  if (ticket !== generation) return
  busy.value = false
  notice.value = copied
    ? 'Copied. Open Chat details → Open → Terminal, choose Workspace if offered, review and paste it yourself.'
    : 'Could not copy. Select the command above and copy it by hand.'
}
</script>

<template>
  <details v-if="rows.length" class="terminal-recovery">
    <summary>Terminal recovery</summary>
    <p>Open Chat details → Open → Terminal, choose Workspace if offered, then review, paste and run the command yourself.</p>
    <div v-for="row in rows" :key="row.action" class="command-row">
      <span>{{ row.action === 'list' ? 'List' : 'Manual sign-in' }}</span>
      <code>{{ row.command }}</code>
      <button type="button" class="btn-small" :disabled="busy" @click="copy(row)">Copy</button>
    </div>
    <p v-if="notice" role="status">{{ notice }}</p>
  </details>
</template>

<style scoped>
.terminal-recovery { min-width: 0; color: var(--fg2); font-size: var(--text-sm); }
summary { cursor: pointer; min-height: var(--touch); display: flex; align-items: center; }
p { margin: var(--space-2) 0; }
.command-row { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); margin: var(--space-2) 0; }
code { min-width: 0; overflow-wrap: anywhere; font-family: var(--font-mono); }
button { min-width: var(--touch); min-height: var(--touch); }
</style>
