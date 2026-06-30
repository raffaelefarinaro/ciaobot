<template>
  <div v-if="subs.length" class="subagent-panel" :class="{ open: panelOpen }">
    <div class="subagent-summary" @click="togglePanel">
      <span class="chevron">{{ panelOpen ? '\u25BE' : '\u25B8' }}</span>
      <span class="icon">&#129302;</span>
      <span class="label">Subagent activity</span>
      <span class="meta">{{ subs.length }} subagent{{ subs.length === 1 ? '' : 's' }}</span>
    </div>
    <div v-if="panelOpen" class="subagent-body">
      <div
        v-for="(sub, i) in subs"
        :key="sub.agent_id"
        class="subagent-block"
        :class="{ open: !!openAgents[i] }"
      >
        <div class="subagent-head" @click="toggleAgent(i)">
          <span class="chevron">{{ openAgents[i] ? '\u25BE' : '\u25B8' }}</span>
          <span class="agent-id">#{{ i + 1 }} · <code>{{ shortId(sub.agent_id) }}</code></span>
          <span class="meta">{{ agentMeta(sub) }}</span>
        </div>
        <div v-if="openAgents[i]" class="subagent-turns">
          <div v-for="(m, j) in sub.messages" :key="j" class="sub-msg" :class="m.role">
            <!-- Activity rollup from _extract_assistant_blocks: tool_name === '_activity' -->
            <div v-if="m.tool_name === '_activity'" class="sub-activity">
              <div
                v-for="(line, k) in m.content.split('\n')"
                :key="k"
                class="sub-activity-line"
                v-text="line"
              ></div>
            </div>
            <div v-else-if="m.role === 'user'" class="sub-bubble user">
              <div class="sub-role">User</div>
              <div class="sub-content" v-html="renderMarkdown(m.content)"></div>
            </div>
            <div v-else-if="m.role === 'assistant'" class="sub-bubble assistant">
              <div class="sub-role">Assistant</div>
              <div class="sub-content" v-html="renderMarkdown(m.content)"></div>
            </div>
            <div v-else class="sub-bubble system">
              <div class="sub-content" v-text="m.content"></div>
            </div>
          </div>
          <div v-if="!sub.messages.length" class="sub-empty">No captured turns.</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { SubagentTranscript } from '../lib/types'
import { renderMarkdown as renderSafeMarkdown } from '../lib/safeMarkdown'

const props = defineProps<{ subagents: SubagentTranscript[] }>()

const subs = computed<SubagentTranscript[]>(() => props.subagents || [])
const panelOpen = ref(false)
const openAgents = ref<Record<number, boolean>>({})

function togglePanel() {
  panelOpen.value = !panelOpen.value
}

function toggleAgent(i: number) {
  openAgents.value = { ...openAgents.value, [i]: !openAgents.value[i] }
}

function shortId(id: string): string {
  // Most SDK ids are UUIDs; show the first 8 chars for readability.
  return id.length > 12 ? `${id.slice(0, 8)}\u2026` : id
}

function agentMeta(sub: SubagentTranscript): string {
  const userTurns = sub.messages.filter(m => m.role === 'user').length
  const assistantTurns = sub.messages.filter(
    m => m.role === 'assistant' && m.tool_name !== '_activity',
  ).length
  const tools = sub.messages.filter(m => m.tool_name === '_activity').length
  const parts: string[] = []
  if (userTurns) parts.push(`${userTurns} prompt${userTurns === 1 ? '' : 's'}`)
  if (assistantTurns) parts.push(`${assistantTurns} repl${assistantTurns === 1 ? 'y' : 'ies'}`)
  if (tools) parts.push(`${tools} tool call${tools === 1 ? '' : 's'}`)
  return parts.join(' \u00B7 ') || 'empty'
}

function renderMarkdown(text: string): string {
  return renderSafeMarkdown(text)
}
</script>

<style scoped>
.subagent-panel {
  align-self: flex-start;
  max-width: 95%;
  width: 95%;
  background: transparent;
  border: 1px dashed var(--border);
  border-left: 3px solid var(--accent2);
  border-radius: var(--radius);
  font-size: 12px;
}

.subagent-summary {
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--fg2);
  line-height: 1.4;
}

.subagent-summary:hover { color: var(--fg); }

.chevron { font-size: 10px; color: var(--fg2); }
.icon { font-size: 14px; }
.label { color: var(--fg2); }
.meta {
  color: var(--fg2);
  opacity: 0.7;
  font-weight: 400;
  margin-left: auto;
  font-size: 11px;
}

.subagent-body {
  padding: 6px 12px 10px;
  border-top: 1px dashed var(--border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.subagent-block {
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
}

.subagent-head {
  padding: 6px 10px;
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fg2);
}

.subagent-head:hover { color: var(--fg); }

.agent-id {
  font-weight: 600;
  color: var(--fg);
}
.agent-id code {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  color: var(--fg2);
}

.subagent-turns {
  padding: 6px 10px 10px;
  border-top: 1px dashed var(--border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sub-bubble {
  padding: 6px 10px;
  border-radius: 4px;
  line-height: 1.45;
}

.sub-bubble.user {
  background: var(--bg3);
  color: var(--fg);
}

.sub-bubble.assistant {
  background: var(--bg2);
  border: 1px solid var(--border);
}

.sub-bubble.system {
  color: var(--fg2);
  font-size: 11px;
  opacity: 0.85;
}

.sub-role {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--fg2);
  margin-bottom: 3px;
}

.sub-content :deep(p) { margin: 3px 0; }
.sub-content :deep(a) {
  color: var(--accent);
  text-decoration: underline;
}
.sub-content :deep(a:hover) {
  color: var(--accent-strong);
}
.sub-content :deep(ul),
.sub-content :deep(ol) {
  padding-left: 22px;
  margin: 3px 0;
  list-style-position: outside;
}
.sub-content :deep(pre) {
  background: var(--bg);
  padding: 6px 8px;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 11px;
}
.sub-content :deep(code) {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
}

.sub-activity {
  background: var(--bg2);
  border-radius: 4px;
  padding: 4px 8px;
  font-size: 11px;
  color: var(--fg2);
}

.sub-activity-line {
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.sub-empty {
  color: var(--fg2);
  font-style: italic;
  font-size: 11px;
  padding: 4px 0;
}
</style>
