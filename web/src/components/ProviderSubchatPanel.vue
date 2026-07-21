<template>
  <div v-if="records.length" class="provider-subchat-panel">
    <div class="panel-header-summary" @click="togglePanel">
      <span class="chevron">{{ panelOpen ? '\u25BE' : '\u25B8' }}</span>
      <span class="icon">&#128172;</span>
      <span class="label">Agent handoffs</span>
      <span class="meta">
        {{ records.length }} handoff{{ records.length === 1 ? '' : 's' }}
      </span>
    </div>

    <div v-if="panelOpen" class="panel-body">
      <div
        v-for="sub in records"
        :key="sub.subchat_id"
        class="subchat-block"
        :class="{ open: !!expanded[sub.subchat_id], terminal: isTerminal(sub.status) }"
      >
        <div class="subchat-head" @click="toggleSubchat(sub.subchat_id)">
          <span class="chevron">{{ expanded[sub.subchat_id] ? '\u25BE' : '\u25B8' }}</span>
          <span class="handoff-route">
            <span class="owner-lbl">{{ sub.owner.label || sub.owner.provider }}</span>
            <span class="arrow">&leftrightarrow;</span>
            <span class="participant-lbl">{{ sub.participant.label || sub.participant.provider }}</span>
            <span class="model-meta">({{ sub.participant.model }})</span>
          </span>
          <span class="status-chip" :class="sub.status">{{ sub.status }}</span>
          <span class="meta-metrics">
            {{ sub.message_count }} msg &middot; {{ formatTime(sub.active_seconds) }}
            <template v-if="sub.input_tokens || sub.output_tokens">
              &middot; {{ formatTokens(sub.input_tokens, sub.output_tokens) }}
            </template>
          </span>
        </div>

        <div v-if="expanded[sub.subchat_id]" class="subchat-content">
          <!-- Control Actions -->
          <div v-if="!isTerminal(sub.status)" class="subchat-actions">
            <button
              v-if="sub.quota_limit_hit"
              type="button"
              class="action-btn extend-btn"
              @click.stop="extend(sub.subchat_id)"
            >
              Extend Limits
            </button>
            <button
              type="button"
              class="action-btn cancel-btn"
              @click.stop="cancel(sub.subchat_id)"
            >
              Cancel Active Work
            </button>
            <button
              type="button"
              class="action-btn close-btn"
              @click.stop="close(sub.subchat_id)"
            >
              Complete Handoff
            </button>
          </div>

          <!-- Transcript Events -->
          <div class="subchat-events">
            <div
              v-for="(ev, index) in getEvents(sub.subchat_id)"
              :key="index"
              class="event-bubble"
              :class="[ev.type, ev.role]"
            >
              <!-- Messages -->
              <template v-if="ev.type === 'message'">
                <div class="event-role">{{ ev.role === 'owner' ? 'Owner' : 'Participant' }}</div>
                <div class="event-text" v-html="renderMarkdown(ev.content)"></div>
              </template>

              <!-- Text Deltas -->
              <template v-else-if="ev.type === 'text_delta'">
                <div class="event-text text-delta">{{ ev.text }}</div>
              </template>

              <!-- Tool use -->
              <template v-else-if="ev.type === 'tool_use'">
                <div class="event-tool-use">
                  Called tool <code>{{ ev.tool_name }}</code>
                </div>
              </template>

              <!-- Permissions -->
              <template v-else-if="ev.type === 'permission_request'">
                <div class="event-permission-card">
                  <div class="card-title">Permission Needed: {{ ev.tool_name }}</div>
                  <pre class="card-detail">{{ ev.message }}</pre>
                  <div class="card-buttons">
                    <button type="button" class="approve" @click="resolvePermission(sub.subchat_id, ev.request_id, true)">Approve</button>
                    <button type="button" class="deny" @click="resolvePermission(sub.subchat_id, ev.request_id, false)">Deny</button>
                  </div>
                </div>
              </template>

              <!-- Structured Questions -->
              <template v-else-if="ev.type === 'question'">
                <div class="event-question-card">
                  <div class="card-title">{{ ev.question }}</div>
                  <div class="options-list">
                    <button
                      v-for="opt in ev.options"
                      :key="opt.value || opt"
                      type="button"
                      class="option-btn"
                      @click="resolveQuestion(sub.subchat_id, ev.request_id, opt.value || opt)"
                    >
                      {{ opt.label || opt }}
                    </button>
                  </div>
                </div>
              </template>

              <!-- Errors -->
              <template v-else-if="ev.type === 'error'">
                <div class="event-error">
                  Error: {{ ev.message }}
                </div>
              </template>
            </div>
            <div v-if="!getEvents(sub.subchat_id).length" class="empty-events">
              Loading handoff events...
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { ProviderSubchatRecord } from '../lib/types'
import { useProjectStore } from '../stores/projects'
import { renderMarkdown as renderSafeMarkdown } from '../lib/safeMarkdown'
import { api } from '../lib/api'

const props = defineProps<{ subchats: ProviderSubchatRecord[] }>()
const store = useProjectStore()

const records = computed(() => props.subchats || [])
const panelOpen = ref(true)
const expanded = ref<Record<string, boolean>>({})

function togglePanel() {
  panelOpen.value = !panelOpen.value
}

async function toggleSubchat(subchatId: string) {
  expanded.value = { ...expanded.value, [subchatId]: !expanded.value[subchatId] }
  if (expanded.value[subchatId]) {
    await store.loadProviderSubchatEvents(subchatId)
  }
}

function getEvents(subchatId: string) {
  return store.providerSubchatEvents[subchatId] || []
}

function isTerminal(status: string): boolean {
  return ['completed', 'cancelled', 'failed', 'interrupted'].includes(status)
}

function formatTime(sec: number): string {
  if (sec < 60) return `${sec.toFixed(0)}s`
  const min = Math.floor(sec / 60)
  const remaining = sec % 60
  return `${min}m ${remaining.toFixed(0)}s`
}

function formatTokens(input: number, output: number): string {
  return `${(input / 1000).toFixed(1)}k in · ${(output / 1000).toFixed(1)}k out`
}

function renderMarkdown(text: string): string {
  return renderSafeMarkdown(text)
}

async function extend(subchatId: string) {
  try {
    await api.post(`/api/provider-subchats/${subchatId}/extend`, { user_authorized: true })
    await store.loadProviderSubchats(store.activeChatId || '')
  } catch (err) {
    console.error('Failed to extend sub-chat limits', err)
  }
}

async function cancel(subchatId: string) {
  try {
    await api.post(`/api/provider-subchats/${subchatId}/cancel`)
    await store.loadProviderSubchats(store.activeChatId || '')
  } catch (err) {
    console.error('Failed to cancel sub-chat', err)
  }
}

async function close(subchatId: string) {
  try {
    await api.post(`/api/provider-subchats/${subchatId}/close`)
    await store.loadProviderSubchats(store.activeChatId || '')
  } catch (err) {
    console.error('Failed to close sub-chat', err)
  }
}

async function resolvePermission(subchatId: string, requestId: string, approved: boolean) {
  try {
    await api.post(`/api/provider-subchats/${subchatId}/permission-response`, {
      request_id: requestId,
      approved,
    })
    await store.loadProviderSubchatEvents(subchatId)
  } catch (err) {
    console.error('Failed to resolve permission', err)
  }
}

async function resolveQuestion(subchatId: string, requestId: string, answer: string) {
  try {
    await api.post(`/api/provider-subchats/${subchatId}/question-response`, {
      request_id: requestId,
      answers: { choice: [answer] },
    })
    await store.loadProviderSubchatEvents(subchatId)
  } catch (err) {
    console.error('Failed to resolve question', err)
  }
}
</script>

<style scoped>
.provider-subchat-panel {
  align-self: flex-start;
  max-width: 95%;
  width: 95%;
  background: var(--bg2);
  border: 1px dashed var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  font-size: 12px;
  margin-top: 8px;
  margin-bottom: 8px;
}

.panel-header-summary {
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--fg);
  line-height: 1.4;
}

.chevron {
  font-size: 10px;
  color: var(--fg2);
}

.icon {
  font-size: 14px;
}

.label {
  color: var(--fg);
}

.meta {
  color: var(--fg2);
  margin-left: auto;
  font-size: 11px;
}

.panel-body {
  padding: 4px 12px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.subchat-block {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}

.subchat-head {
  padding: 8px 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  user-select: none;
}

.handoff-route {
  font-weight: 600;
  color: var(--fg);
  display: flex;
  align-items: center;
  gap: 4px;
}

.arrow {
  color: var(--fg2);
}

.model-meta {
  font-weight: 400;
  color: var(--fg2);
  font-size: 11px;
}

.status-chip {
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
}

.status-chip.created { background: var(--bg2); color: var(--fg2); }
.status-chip.running { background: #e0f2fe; color: #0369a1; }
.status-chip.waiting_owner { background: #fef3c7; color: #b45309; }
.status-chip.completed { background: #dcfce7; color: #15803d; }
.status-chip.cancelled { background: #f3f4f6; color: #4b5563; }
.status-chip.failed { background: #fee2e2; color: #b91c1c; }
.status-chip.interrupted { background: #ffedd5; color: #c2410c; }

.meta-metrics {
  color: var(--fg2);
  font-size: 11px;
  margin-left: auto;
}

.subchat-content {
  border-top: 1px solid var(--border);
  padding: 12px;
  background: var(--bg2);
}

.subchat-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.action-btn {
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  font-weight: 600;
  border: 1px solid var(--border);
  cursor: pointer;
  background: var(--bg);
  color: var(--fg);
  font-size: 11px;
}

.action-btn:hover {
  background: var(--bg2);
}

.extend-btn {
  background: #f59e0b;
  color: white;
  border-color: #d97706;
}
.extend-btn:hover {
  background: #d97706;
}

.subchat-events {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 300px;
  overflow-y: auto;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}

.event-bubble {
  padding: 8px 12px;
  border-radius: var(--radius);
  max-width: 85%;
  line-height: 1.4;
}

.event-bubble.message.owner {
  align-self: flex-end;
  background: var(--accent2-bg);
  color: var(--fg);
  border-bottom-right-radius: 0;
}

.event-bubble.message.participant {
  align-self: flex-start;
  background: var(--bg2);
  color: var(--fg);
  border-bottom-left-radius: 0;
}

.event-role {
  font-weight: 700;
  font-size: 10px;
  color: var(--fg2);
  margin-bottom: 4px;
}

.event-tool-use, .event-error {
  align-self: center;
  font-style: italic;
  color: var(--fg2);
  font-size: 11px;
}

.event-error {
  color: var(--error);
}

.event-permission-card, .event-question-card {
  align-self: flex-start;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  padding: 10px;
  border-radius: var(--radius-sm);
  width: 100%;
}

.card-title {
  font-weight: 700;
  margin-bottom: 6px;
}

.card-detail {
  font-family: monospace;
  font-size: 10px;
  background: var(--bg);
  padding: 6px;
  border-radius: 4px;
  margin-bottom: 8px;
  white-space: pre-wrap;
}

.card-buttons, .options-list {
  display: flex;
  gap: 6px;
}

.card-buttons button, .option-btn {
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid var(--border);
}

.card-buttons button.approve {
  background: var(--accent);
  color: white;
  border-color: var(--accent);
}

.card-buttons button.deny {
  background: var(--error-bg);
  color: var(--error);
  border-color: var(--border);
}

.empty-events {
  color: var(--fg2);
  font-style: italic;
  text-align: center;
  padding: 16px;
}
</style>
