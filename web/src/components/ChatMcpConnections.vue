<script setup lang="ts">
import { computed, nextTick, onMounted, ref, useId, watch } from 'vue'
import type { ChatMcpRow } from '../composables/useChatMcpConnections'
import { mcpAuthBusy, useMcpAuthStart } from '../composables/useMcpAuthStart'
import { mcpServerName } from '../lib/chatActions'
import type { ChatAction } from '../lib/types'
import { useProjectStore } from '../stores/projects'
import NeedsYouCard from './NeedsYouCard.vue'

const props = defineProps<{
  chatId: string
  provider: 'claude' | 'opencode'
  controller: ReturnType<typeof import('../composables/useChatMcpConnections').useChatMcpConnections>
  prioritizeHeading?: boolean
}>()
const emit = defineEmits<{ settings: [] }>()
const store = useProjectStore()
const root = ref<HTMLElement | null>(null)
const headingId = useId()
const notice = ref('')
const error = ref('')
const serverName = ref('')
const startController = useMcpAuthStart({
  chatId: () => props.chatId,
  provider: () => props.provider,
  server: () => serverName.value,
  onNotice: value => { notice.value = value },
  onError: value => { error.value = value },
})
const rows = computed(() => props.controller.rows.value)
const inventoryAvailable = computed(() => {
  const inventories = props.controller.status.value?.providers.filter(item => item.provider === props.provider) ?? []
  return inventories.length > 0 && inventories.every(item => item.available && item.inventory_state === 'loaded')
})
function selectedAction(row: ChatMcpRow): ChatAction | null {
  return (store.chatActions[props.chatId] ?? []).find(a => a.id === row.action?.id) ?? row.action
}
function canStart(row: ChatMcpRow): boolean {
  return props.controller.canStart(row)
}
async function signIn(row: ChatMcpRow) {
  if (!canStart(row)) return
  notice.value = ''
  error.value = ''
  serverName.value = row.name
  await startController.start()
}
function focusSection() { (root.value?.querySelector('.chat-mcp-heading') as HTMLElement | null)?.focus({ preventScroll: true }) }
function revealDrawerAction() {
  if (props.prioritizeHeading) return
  const rail = root.value?.closest<HTMLElement>('.chat-rail--drawer')
  const card = root.value?.querySelector<HTMLElement>('.needs-you-card')
  if (!rail || !card) return
  const bounds = rail.getBoundingClientRect()
  const target = card.getBoundingClientRect()
  if (target.top < bounds.top || target.bottom > bounds.bottom) {
    rail.scrollTo({ top: Math.max(0, rail.scrollTop + target.top - bounds.top), behavior: 'auto' })
  }
}
defineExpose({ focusSection, revealDrawerAction })
onMounted(() => { void nextTick(revealDrawerAction) })
watch(() => rows.value.map(row => [row.name, row.action?.id] as const), async (next, previous) => {
  const removed = new Set(previous.flatMap(([name, id]) => id && !next.some(([nextName, nextId]) => name === nextName && id === nextId) ? [id] : []))
  const activeCard = (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('.needs-you-card')
  if (activeCard && removed.has(activeCard.dataset.actionId ?? '')) {
    await nextTick()
    focusSection()
  }
  if (next.some(([name, id]) => id && !previous.some(([previousName, previousId]) => name === previousName && id === previousId))) {
    await nextTick()
    revealDrawerAction()
  }
}, { flush: 'pre' })
</script>

<template>
  <section ref="root" class="chat-mcp-connections cd-section cd-block" :aria-labelledby="headingId">
    <div class="cd-head">
      <h3 :id="headingId" class="cd-heading chat-mcp-heading" tabindex="-1">MCP</h3>
      <span v-if="controller.problemCount.value" class="cd-count">{{ controller.problemCount.value }}</span>
      <span class="chat-mcp-provider">{{ provider === 'claude' ? 'Claude' : 'OpenCode' }}</span>
      <button type="button" class="cd-action chat-mcp-refresh" @click="controller.refresh">Refresh</button>
      <button type="button" class="cd-action chat-mcp-settings" title="Opens MCP settings for this chat's workspace and CLI" @click="emit('settings')">Chat workspace MCP settings</button>
    </div>
    <p v-if="controller.loading.value && !controller.status.value" class="chat-mcp-message" role="status">Loading MCP connections…</p>
    <p v-if="controller.error.value" class="chat-mcp-message" role="status">{{ controller.error.value }} <button class="cd-action" @click="controller.refresh">Retry</button></p>
    <p v-else-if="controller.status.value && !inventoryAvailable" class="chat-mcp-message">This provider’s MCP inventory is unavailable or unchecked.</p>
    <p v-else-if="controller.status.value && !rows.length" class="chat-mcp-message">No MCP servers configured for this provider.</p>
    <p v-if="!controller.status.value && !controller.error.value && !controller.loading.value" class="chat-mcp-message">Connection status is unchecked.</p>
    <div v-if="rows.length" class="chat-mcp-rows">
      <div v-for="row in rows" :key="row.name" class="chat-mcp-row">
        <span class="chat-mcp-name" :title="row.name">{{ mcpServerName(row.name) }}</span>
        <span class="chat-mcp-state">{{ row.enabled === null ? 'Configuration unknown' : row.state === 'connected' ? 'Connected' : row.state === 'needs_auth' ? 'Needs sign-in' : row.state === 'failed' ? 'Connection failed' : row.state === 'disabled' ? 'Disabled' : 'Unchecked' }}</span>
        <p v-if="row.server?.allowed === false" class="chat-mcp-note">Blocked by this chat’s tool permissions.</p>
        <p v-else-if="row.server?.scope === 'claudeai'" class="chat-mcp-note">This Claude.ai connection is managed outside Ciaobot.</p>
        <p v-else-if="!row.server && row.action" class="chat-mcp-note">Configuration and enablement are unknown for this action.</p>
        <p v-if="row.error" class="chat-mcp-error">{{ row.error }}</p>
        <NeedsYouCard v-if="selectedAction(row) && row.enabled !== false" :key="selectedAction(row)!.id" :chat-id="chatId" :action="selectedAction(row)!" />
        <button v-else-if="canStart(row)" type="button" class="btn-small btn-primary chat-mcp-sign-in" :disabled="mcpAuthBusy(chatId, provider, row.name)" @click="signIn(row)">Sign in</button>
        <p v-if="selectedAction(row)?.state === 'failed' && !row.error" class="chat-mcp-error">Connection failed. Check the provider settings.</p>
      </div>
    </div>
    <p v-if="notice" class="chat-mcp-feedback" role="status">{{ notice }}</p>
    <p v-if="error" class="chat-mcp-feedback" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped src="./chatDetails.css"></style>
<style scoped>
.chat-mcp-connections { border-top: 1px solid var(--border); padding-top: var(--space-3); }
.chat-mcp-connections .cd-head { flex-wrap: wrap; min-width: 0; }
.chat-mcp-provider { margin-left: auto; color: var(--fg3); font-size: var(--text-xs); }
.chat-mcp-refresh, .chat-mcp-settings { min-height: 44px; }
.chat-mcp-rows { display: grid; }
.chat-mcp-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-1) var(--space-2); padding: var(--space-2) 0; border-bottom: 1px solid var(--border); }
.chat-mcp-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.chat-mcp-state { color: var(--fg3); font-size: var(--text-sm); }
.chat-mcp-error, .chat-mcp-message, .chat-mcp-feedback, .chat-mcp-note { grid-column: 1 / -1; margin: 0; color: var(--fg2); overflow-wrap: anywhere; }
.chat-mcp-error { color: var(--error); }
.chat-mcp-row :deep(.needs-you-card) { grid-column: 1 / -1; }
.chat-mcp-sign-in { grid-column: 1 / -1; min-height: 44px; }
.chat-mcp-heading:focus { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (pointer: coarse) { .chat-mcp-refresh, .chat-mcp-settings, .chat-mcp-sign-in { min-height: 44px; } }
</style>
