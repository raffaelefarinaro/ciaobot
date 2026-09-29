<template>
  <!-- What the agent is given for this chat, each with when it is sent. Shared
       by the Work details rail and the narrow-pane drawer. -->
  <section class="rail-section agent-context" aria-labelledby="agent-context-label">
    <p id="agent-context-label" class="rail-label">Agent context</p>
    <template v-if="contextPct != null">
      <div
        class="agent-context-meter"
        role="meter"
        :aria-valuenow="contextPct"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-label="Context window used"
      >
        <span class="agent-context-track"><span class="agent-context-fill" :style="{ width: `${Math.min(100, contextPct)}%` }"></span></span>
        <strong>{{ contextPctLabel }}</strong>
      </div>
      <p class="rail-note agent-context-meter-note">of the model's context window, as of the last reply.</p>
    </template>
    <div class="rail-list">
      <button
        v-if="guide.path"
        type="button"
        class="rail-item agent-context-row"
        :title="`Open ${guide.path}`"
        @click="emit('open-file', guide.path)"
      >
        <span class="agent-context-row-top">
          <span class="agent-context-name">{{ guideName }}</span>
          <span class="agent-context-meta">{{ formatTokens(guideTokens) }} tokens</span>
        </span>
        <small>Workspace guide · read every turn</small>
      </button>
      <div v-else-if="guide.error" class="rail-item agent-context-row agent-context-error" role="status">
        <span class="agent-context-row-top">
          <span class="agent-context-name">Workspace guide</span>
          <button type="button" class="agent-context-link" @click="loadGuide">Retry</button>
        </span>
        <small>Could not be read just now: {{ guide.error }}</small>
      </div>
      <!-- General with no description or doc sends no brief at all. -->
      <div v-if="project && briefLines.length" class="rail-item agent-context-row agent-context-brief">
        <span class="agent-context-row-top">
          <router-link :to="`/project/${project.project_id}`" class="agent-context-name agent-context-project">{{ project.name }}</router-link>
          <span class="agent-context-meta">{{ formatTokens(briefTokens) }} tokens</span>
        </span>
        <small>Project brief, sent at the start and when it changes:</small>
        <pre class="agent-context-brief-text"><template v-for="line in briefLines" :key="line.key">{{ line.prefix }}<button
          v-if="line.path"
          type="button"
          class="agent-context-link"
          :title="`Open ${line.path}`"
          @click="emit('open-file', line.path)"
        >{{ line.value }}</button><template v-else>{{ line.value }}</template>
</template></pre>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ProjectInfo } from '../lib/types'
import { fetchWorkspaceGuide, formatTokens, tokensFor, type WorkspaceGuide } from '../lib/workspaceGuide'

const props = defineProps<{
  project: ProjectInfo | null | undefined
  /** Percent of the context window used as of the last reply, when the provider reported it. */
  contextPct: number | null
}>()
const emit = defineEmits<{ 'open-file': [path: string] }>()

const guide = ref<WorkspaceGuide>({ path: '', content: '', error: '' })
let guideSeq = 0
// A failed read (network, non-404) keeps its error so the row says the guide
// is unreadable rather than disappearing as if the workspace had none.
async function loadGuide() {
  const workspace = props.project?.workspace
  const seq = ++guideSeq
  if (!workspace) { guide.value = { path: '', content: '', error: '' }; return }
  const result = await fetchWorkspaceGuide(workspace)
  if (seq === guideSeq) guide.value = result
}
watch(() => props.project?.workspace, loadGuide, { immediate: true })
const guideName = computed(() => guide.value.path.split('/').pop() || 'AGENTS.md')
const guideTokens = computed(() => tokensFor(guide.value.content.length))

// The brief is the capsule's stable project lines (ciao/context/capsule.py),
// rendered as sent: the name (General is implicit), the description and the
// canonical doc's path. Not the doc itself. `field` mirrors capsule._field.
function field(value: string, limit = 1200): string {
  return value.split(/\s+/).filter(Boolean).join(' ').slice(0, limit)
}
const briefLines = computed(() => {
  const p = props.project
  if (!p) return []
  const lines: { key: string; prefix: string; value: string; path?: string }[] = []
  if (p.name && p.name !== 'General') lines.push({ key: 'project', prefix: 'project=', value: `"${field(p.name, 180)}"` })
  if (p.context) lines.push({ key: 'project_context', prefix: 'project_context=', value: field(p.context) })
  if (p.vault_doc_path) lines.push({ key: 'canonical_doc', prefix: 'canonical_doc=', value: field(p.vault_doc_path, 300), path: p.vault_doc_path })
  return lines
})
const briefTokens = computed(() => tokensFor(briefLines.value.map((l) => l.prefix + l.value).join('\n').length))

const contextPctLabel = computed(() => {
  const pct = props.contextPct
  if (pct == null) return ''
  return `${pct < 10 ? Math.round(pct * 10) / 10 : Math.round(pct)}%`
})

</script>

<style scoped>
.agent-context-meter {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin: 4px 0 0;
}
.agent-context-track {
  flex: 1;
  min-width: 0;
  height: 6px;
  border-radius: 999px;
  background: var(--bg3);
  overflow: hidden;
}
.agent-context-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--fg2);
}
.agent-context-meter strong { font-variant-numeric: tabular-nums; }
.agent-context-meter-note { margin: 2px 0 10px; }
.agent-context-row { gap: 1px; }
.agent-context-row-top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
}
.agent-context-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}
.agent-context-meta {
  flex: none;
  color: var(--fg3);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
.agent-context-row small { line-height: 1.4; }
.agent-context-brief { cursor: default; }
.agent-context-brief:hover { color: var(--fg); }
.agent-context-brief-text {
  margin: 4px 0 0;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--bg3);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.agent-context-link {
  padding: 0;
  border: 0;
  background: none;
  color: var(--fg);
  font: inherit;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 2px;
  cursor: pointer;
}
.agent-context-brief-text .agent-context-link {
  display: inline;
  text-align: left;
  color: inherit;
}
.agent-context-link:hover,
.agent-context-project:hover { text-decoration-color: currentColor; }
.agent-context-project {
  color: var(--fg);
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
</style>
