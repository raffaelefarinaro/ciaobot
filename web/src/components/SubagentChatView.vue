<template>
  <div class="subagent-view">
    <PaneHeader :brand="false" @open-sidebar="emit('open-sidebar')">
      <template #title>
        <div class="header-left">
          <RouterLink
            :to="`/chat/${chatId}`"
            class="btn-icon back-btn"
            :aria-label="`Back to ${parentTitle}`"
            :title="`Back to ${parentTitle}`"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M15 6l-6 6 6 6" />
            </svg>
          </RouterLink>
          <div class="subagent-crumb">
            <span class="pane-title agent-name">{{ agentLabel }}</span>
            <span class="parent-crumb" :title="parentTitle">in {{ parentTitle }}</span>
          </div>
        </div>
      </template>
      <template #actions>
        <span class="ro-tag" title="Subagent transcripts are a record, not a session you can steer">Read only</span>
      </template>
    </PaneHeader>

    <div class="subagent-body">
      <div class="page-grid subagent-grid">
        <div class="page-main subagent-main">
          <p v-if="loading && !subagent" class="subagent-loading" role="status">Loading the subagent transcript…</p>
          <section v-else-if="!subagent" class="subagent-empty" role="status" aria-labelledby="subagent-empty-title">
            <h2 id="subagent-empty-title">Transcript not on this machine</h2>
            <p>
              This subagent's transcript is not available on this machine. Finished
              subagents keep their steps in the parent chat's Activity.
              <RouterLink :to="`/chat/${chatId}`" class="subagent-empty-link">Open the parent chat</RouterLink>
            </p>
          </section>
          <template v-else>
            <!-- The key carries the message's own shape, not just its index: the
                 poll replaces the whole transcript every few seconds, and any
                 change that is not a pure append shifts every later index. On a
                 bare index key Vue patches the existing nodes in place, so an
                 already-rendered bubble takes on a different message's role branch
                 and v-html. The index stays in the key so it is unique even when
                 two messages are genuinely identical. -->
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
                  Prompt
                  <!-- Both provider renderers omit `timestamp` on subagent
                       messages, so it is drawn only when one is actually there. -->
                  <span v-if="m.timestamp" class="bubble-time">{{ m.timestamp }}</span>
                </div>
                <div class="bubble-content" v-html="renderMarkdown(m.content)"></div>
              </div>
              <div v-else-if="m.role === 'assistant'" class="bubble assistant">
                <div v-if="m.timestamp" class="bubble-role">
                  <span class="bubble-time">{{ m.timestamp }}</span>
                </div>
                <div class="bubble-content" v-html="renderMarkdown(m.content)"></div>
              </div>
              <div v-else class="bubble system">
                <div class="bubble-content" v-text="m.content"></div>
              </div>
            </div>
            <p v-if="!subagent.messages.length" class="subagent-loading">No captured turns.</p>
          </template>
        </div>

        <aside class="page-rail subagent-rail" aria-labelledby="subagent-rail-title">
          <h2 id="subagent-rail-title" class="rail-title">Run</h2>
          <div class="rail-kvs">
            <div class="rail-kv"><span>Agent</span><strong>{{ agentLabel }}</strong></div>
            <div v-if="subagent?.subagent_type" class="rail-kv">
              <span>Type</span><strong class="type-value">{{ subagent.subagent_type }}</strong>
            </div>
            <div v-if="status" class="rail-kv">
              <span>State</span>
              <strong class="status-value" :class="status">
                <span v-if="status === 'running'" class="running-spinner" aria-hidden="true" />
                {{ statusLabel }}
              </strong>
            </div>
            <div v-if="subagent" class="rail-kv"><span>Messages</span><strong>{{ subagent.messages.length }}</strong></div>
          </div>
          <p class="rail-note">Started from {{ parentTitle }}.</p>
        </aside>
      </div>
    </div>

    <!-- Claude Code subagents are transcript files, not resumable sessions, so
         there is nothing to send into. A note bar stands where the composer
         would be, saying where replies go instead of showing a dead input. -->
    <div class="readonly-bar">
      <div class="readonly-note">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="5" y="11" width="14" height="9" rx="2" />
          <path d="M8 11V8a4 4 0 0 1 8 0v3" />
        </svg>
        <span>Replies go to the parent chat.</span>
        <RouterLink :to="`/chat/${chatId}`" class="btn-small readonly-action">Go to chat</RouterLink>
      </div>
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

const statusLabel = computed(() => {
  const s = status.value
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''
})

async function refresh(): Promise<void> {
  loading.value = true
  try {
    // Fetch this agent alone. Only if that turns up nothing do we pay for the
    // whole set — which covers ids the narrow fetch cannot resolve on its own
    // (an opencode child, or a stale link to an agent the parent never named).
    await store.loadSubagent(props.chatId, props.agentId)
    // A row with no messages counts as a miss too, not just a missing row: a
    // host that answers the narrowed fetch with an empty transcript would
    // otherwise pin "No captured turns" here forever, because the row exists.
    // Off the poll timer, so an agent that genuinely has no turns costs one
    // extra fetch per visit, not one every four seconds.
    if (!subagent.value?.messages.length) await store.loadSubagents(props.chatId)
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
  store.setSubagentViewActive(props.chatId)
})

onMounted(() => {
  store.setSubagentViewActive(props.chatId)
  void refresh()
})
onBeforeUnmount(() => {
  store.setSubagentViewActive(null)
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

.back-btn { color: var(--fg3); text-decoration: none; }
.back-btn:hover { color: var(--fg); }

/* Agent name as the title, then the parent chat as a muted crumb, on one
   line like the chat header's title + project. */
.subagent-crumb {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
  flex: 1;
}
.subagent-crumb .agent-name { flex: 0 1 auto; }
.parent-crumb {
  min-width: 0;
  flex: 0 1 auto;
  color: var(--fg3);
  font-size: var(--text-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Squared state tag (DESIGN.md: --radius-xs), sentence case. */
.ro-tag {
  flex: none;
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 0 6px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-size: var(--text-xs, 11px);
  font-weight: 600;
  white-space: nowrap;
}

.subagent-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.subagent-grid {
  padding-top: var(--space-6);
  padding-bottom: var(--space-6);
}
.subagent-main {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.subagent-loading {
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
}

/* Same shape as the chat's "Start with a request" empty state. */
.subagent-empty { max-width: 640px; padding: var(--space-5) 0; }
.subagent-empty h2 {
  margin: 0 0 4px;
  color: var(--fg);
  font-size: calc(20px * var(--font-scale));
  font-weight: 650;
  line-height: 1.2;
  letter-spacing: -0.02em;
}
.subagent-empty p {
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.5;
}
.subagent-empty-link { color: var(--accent); }

.subagent-rail .rail-kv strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.status-value {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.status-value.failed { color: var(--error); }

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

.bubble {
  max-width: 90%;
  padding: 8px 12px;
  border-radius: var(--radius-lg, 12px);
  line-height: 1.6;
}

.bubble.user {
  align-self: flex-end;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  background: color-mix(in srgb, var(--accent) 9%, transparent);
  color: var(--fg);
}

.bubble.assistant {
  align-self: stretch;
  max-width: 100%;
  padding: 0;
}

.bubble.system {
  align-self: flex-start;
  color: var(--fg3);
  font-size: var(--text-sm);
}

.bubble-role {
  display: flex;
  gap: 8px;
  font-size: var(--text-xs, 11px);
  font-weight: 600;
  color: var(--fg3);
  margin-bottom: 2px;
}

.bubble-content :deep(p) { margin: 4px 0; }
.bubble-content :deep(a) { color: var(--accent); text-decoration: underline; }
.bubble-content :deep(ul),
.bubble-content :deep(ol) { padding-left: 22px; margin: 4px 0; }
.bubble-content :deep(pre) {
  background: var(--bg2);
  padding: 6px 8px;
  border-radius: var(--radius-sm, 6px);
  overflow-x: auto;
}
.bubble-content :deep(code) {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12px;
}

/* Tool activity as a quiet hairline step, like the chat's turn lines. */
.sub-activity {
  align-self: stretch;
  padding: 6px 0 6px 12px;
  border-left: 1px solid var(--border);
  font-size: var(--text-sm);
  color: var(--fg2);
}

.sub-activity-line {
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

/* Sits on the page grid's column edges, where the composer sits in a chat. */
.readonly-bar {
  box-sizing: border-box;
  width: 100%;
  max-width: var(--page-max);
  margin: 0 auto;
  padding: 0 var(--page-gutter) var(--space-4);
}
.readonly-note {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 8px 8px 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg2);
  font-size: var(--text-sm);
}
.readonly-note > svg { flex: none; color: var(--fg3); }
.readonly-note > span { flex: 1; min-width: 0; }
.readonly-action {
  flex: none;
  min-height: var(--touch);
}
@media (pointer: fine) {
  .readonly-action { min-height: 34px; padding-block: 6px; }
}
</style>
