<template>
  <div class="subagent-view">
    <PaneHeader page-tag="subagent" @open-sidebar="emit('open-sidebar')">
      <template #title>
        <RouterLink :to="`/chat/${chatId}`" class="parent-link" :title="parentTitle">
          <span aria-hidden="true">&#8592;</span> {{ parentTitle }}
        </RouterLink>
      </template>
      <template #actions>
        <span class="ro-chip" title="Subagent transcripts are a record, not a session you can steer">read-only</span>
      </template>
    </PaneHeader>

    <div class="subagent-meta">
      <span v-if="subagent?.subagent_type" class="type-chip">{{ subagent.subagent_type }}</span>
      <span class="agent-name">{{ agentLabel }}</span>
      <span v-if="status" class="status-chip" :class="status">{{ status }}</span>
      <span v-if="status === 'running'" class="running-spinner" aria-hidden="true" />
    </div>

    <div class="subagent-body">
      <p v-if="loading && !subagent" class="notice" role="status">Loading the subagent transcript…</p>
      <p v-else-if="!subagent" class="notice" role="status">
        This subagent's transcript is not available on this machine. Completed
        subagents stay in the chat's Activity trace.
      </p>
      <template v-else>
        <!-- Keyed by role+content, not index: the poll replaces the whole
             transcript every few seconds, and any change that is not a pure
             append shifts every later index. With index keys Vue patches the
             nodes in place instead of moving them, so an already-rendered
             bubble takes on a different message's role branch and v-html. -->
        <div
          v-for="(m, i) in subagent.messages"
          :key="`${i}:${m.role}:${m.tool_name || ''}:${m.content.length}`"
          class="sub-msg"
          :class="m.role"
        >
          <!-- Activity rollup from _extract_assistant_blocks: tool_name === '_activity' -->
          <div v-if="m.tool_name === '_activity'" class="sub-activity">
            <div
              v-for="(line, k) in m.content.split('\n')"
              :key="k"
              class="sub-activity-line"
              v-text="line"
            ></div>
          </div>
          <div v-else-if="m.role === 'user'" class="bubble user">
            <div class="bubble-role">
              User
              <!-- Both provider renderers omit `timestamp` on subagent
                   messages, so it is drawn only when one is actually there. -->
              <span v-if="m.timestamp" class="bubble-time">{{ m.timestamp }}</span>
            </div>
            <div class="bubble-content" v-html="renderMarkdown(m.content)"></div>
          </div>
          <div v-else-if="m.role === 'assistant'" class="bubble assistant">
            <div class="bubble-role">
              Assistant
              <span v-if="m.timestamp" class="bubble-time">{{ m.timestamp }}</span>
            </div>
            <div class="bubble-content" v-html="renderMarkdown(m.content)"></div>
          </div>
          <div v-else class="bubble system">
            <div class="bubble-content" v-text="m.content"></div>
          </div>
        </div>
        <p v-if="!subagent.messages.length" class="notice">No captured turns.</p>
      </template>
    </div>

    <!-- Claude Code subagents are transcript files, not resumable sessions, so
         there is nothing to send into. The composer is kept (disabled) rather
         than dropped so the view still reads as a chat and the difference is
         stated where the user would otherwise type. -->
    <div class="composer">
      <textarea
        class="composer-input"
        disabled
        rows="1"
        aria-label="Replying to a subagent is not possible"
        placeholder="Read-only — reply in the parent chat instead"
      ></textarea>
      <RouterLink :to="`/chat/${chatId}`" class="btn-small composer-action">
        Go to chat
      </RouterLink>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import PaneHeader from './PaneHeader.vue'
import { useProjectStore } from '../stores/projects'
import { renderMarkdown as renderSafeMarkdown } from '../lib/safeMarkdown'
import type { SubagentTranscript } from '../lib/types'
import { sameAgent, shortAgentId } from '../lib/subagentIds'

const props = defineProps<{ chatId: string; agentId: string }>()
const emit = defineEmits<{ 'open-sidebar': [] }>()

const store = useProjectStore()
const loading = ref(false)

const subagent = computed<SubagentTranscript | null>(() => {
  const rows = store.subagents[props.chatId] || []
  return rows.find(s => sameAgent(s.agent_id, props.agentId)) || null
})

const parentTitle = computed(
  () => store.chats.find(c => c.chat_id === props.chatId)?.title || 'Chat',
)

const agentLabel = computed(
  () => (subagent.value?.description || '').trim() || shortAgentId(props.agentId),
)

// The sidebar row only exists while the agent runs, but this view is also
// reachable from the in-chat panel long after it finished, so fall back to the
// transcript's own status.
const status = computed(() => {
  const live = store.runningSubagentsFor(props.chatId)
    .some(s => sameAgent(s.agent_id, props.agentId))
  return live ? 'running' : (subagent.value?.status || '')
})

async function refresh(): Promise<void> {
  loading.value = true
  try {
    // Fetch this agent alone. Only if that turns up nothing do we pay for the
    // whole set — which covers ids the narrow fetch cannot resolve on its own
    // (an opencode child, or a stale link to an agent the parent never named).
    await store.loadSubagent(props.chatId, props.agentId)
    if (!subagent.value) await store.loadSubagents(props.chatId)
  } finally {
    loading.value = false
  }
}

// Poll while the agent is still working: its transcript file grows as it goes,
// so this is a live feed for the same reason the in-chat panel polls. Only
// this agent is re-fetched — the unfiltered endpoint renders every subagent
// the chat ever spawned, which is far too much to put on a 4s timer.
//
// Bounded, and paused while the tab is hidden. A parent turn killed mid-
// dispatch never writes the record that moves the transcript's status off
// "running", so an unbounded timer polled a dead agent every four seconds for
// as long as the tab stayed open — each poll a full parse of the parent
// session file. The store's sidebar poll already bounds itself the same way.
const POLL_MS = 4000
const POLL_LIMIT_MS = 15 * 60 * 1000
let timer: ReturnType<typeof setInterval> | null = null
let pollingSince = 0

function stopPolling(): void {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

function startPolling(): void {
  stopPolling()
  if (status.value !== 'running') return
  if (document.visibilityState !== 'visible') return
  if (!pollingSince) pollingSince = Date.now()
  timer = setInterval(() => {
    if (Date.now() - pollingSince > POLL_LIMIT_MS) {
      stopPolling()
      return
    }
    void store.loadSubagent(props.chatId, props.agentId)
  }, POLL_MS)
}

watch(status, (value) => {
  // A run that genuinely restarts gets a fresh budget; a stuck one does not.
  if (value !== 'running') pollingSince = 0
  startPolling()
}, { immediate: true })

function onVisibilityChange(): void {
  if (document.visibilityState === 'visible') startPolling()
  else stopPolling()
}
document.addEventListener('visibilitychange', onVisibilityChange)

watch(() => [props.chatId, props.agentId], () => {
  pollingSince = 0
  void refresh()
})

onMounted(() => { void refresh() })
onBeforeUnmount(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

function renderMarkdown(text: string): string {
  return renderSafeMarkdown(text)
}
</script>

<style scoped>
.subagent-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  background: var(--bg);
}

.parent-link {
  /* The only in-header way back to the parent chat, and the first
     touch-reachable control in a PaneHeader title slot — PaneHeader's own 44px
     compensation is scoped to its icon buttons and does not reach this. */
  display: inline-flex;
  align-items: center;
  min-height: var(--touch, 44px);
  color: var(--fg2);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.parent-link:hover { color: var(--fg); }

.ro-chip,
.type-chip,
.status-chip {
  flex: none;
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--fg2);
  font-size: var(--text-xs, 11px);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.status-chip.running { color: var(--accent2, var(--accent)); border-color: var(--accent2, var(--accent)); }
.status-chip.failed { color: var(--error, #e5484d); border-color: var(--error, #e5484d); }

.subagent-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
  color: var(--fg2);
  font-size: var(--text-sm, 13px);
}

.agent-name {
  font-weight: 600;
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.running-spinner {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent2, var(--accent));
  animation: subagent-view-pulse 1.1s ease-in-out infinite;
}

@keyframes subagent-view-pulse {
  0%, 100% { transform: scale(0.55); opacity: 0.35; }
  50% { transform: scale(1); opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .running-spinner { animation-duration: 2.2s; }
}

.subagent-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.notice {
  color: var(--fg2);
  font-style: italic;
}

.bubble {
  max-width: 90%;
  padding: 8px 12px;
  border-radius: var(--radius, 6px);
  line-height: 1.5;
}

.bubble.user {
  align-self: flex-end;
  background: var(--bg3);
  color: var(--fg);
}

.bubble.assistant {
  align-self: flex-start;
  background: var(--bg2);
  border: 1px solid var(--border);
}

.bubble.system {
  align-self: flex-start;
  color: var(--fg2);
  font-size: var(--text-sm, 12px);
}

.bubble-role {
  display: flex;
  gap: 8px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--fg2);
  margin-bottom: 3px;
}

.bubble-time { text-transform: none; letter-spacing: 0; }

.bubble-content :deep(p) { margin: 4px 0; }
.bubble-content :deep(a) { color: var(--accent); text-decoration: underline; }
.bubble-content :deep(ul),
.bubble-content :deep(ol) { padding-left: 22px; margin: 4px 0; }
.bubble-content :deep(pre) {
  background: var(--bg);
  padding: 6px 8px;
  border-radius: 4px;
  overflow-x: auto;
}
.bubble-content :deep(code) {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12px;
}

.sub-activity {
  align-self: flex-start;
  max-width: 90%;
  background: var(--bg2);
  border-radius: 4px;
  padding: 4px 8px;
  font-size: var(--text-sm, 12px);
  color: var(--fg2);
}

.sub-activity-line {
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.composer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
}

.composer-input {
  flex: 1;
  min-width: 0;
  min-height: var(--touch);
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius, 6px);
  background: var(--bg2);
  color: var(--fg2);
  font: inherit;
  resize: none;
  cursor: not-allowed;
}

.composer-action {
  flex: none;
  min-height: var(--touch);
  display: inline-flex;
  align-items: center;
  text-decoration: none;
}
</style>
