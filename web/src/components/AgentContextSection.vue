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
      <div v-if="project && briefText" class="rail-item agent-context-row agent-context-brief">
        <span class="agent-context-row-top">
          <router-link :to="`/project/${project.project_id}`" class="agent-context-name agent-context-project">{{ project.name }}</router-link>
          <span class="agent-context-meta">{{ formatTokens(briefTokens) }} tokens</span>
        </span>
        <p
          v-if="project.context"
          class="agent-context-description"
          @click="openContextFile"
          v-html="contextHtml"
        ></p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ProjectInfo } from '../lib/types'
import { linkifyText } from '../lib/filePaths'
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

// The brief is the capsule's stable project lines (ciao/context/capsule.py).
// Count the capsule's sent text, not the readable presentation below the
// project name. `field` mirrors capsule._field.
function field(value: string, limit = 1200): string {
  return value.split(/\s+/).filter(Boolean).join(' ').slice(0, limit)
}
const briefText = computed(() => {
  const p = props.project
  if (!p) return ''
  const lines: string[] = []
  if (p.name && p.name !== 'General') lines.push(`project="${field(p.name, 180)}"`)
  if (p.context) lines.push(`project_context=${field(p.context)}`)
  if (p.vault_doc_path) lines.push(`canonical_doc=${field(p.vault_doc_path, 300)}`)
  return lines.join('\n')
})
const briefTokens = computed(() => tokensFor(briefText.value.length))

// Plain text is escaped by the shared linker; a canonical filename resolves
// to its full path without adding another copy below the description.
const contextHtml = computed(() => linkifyText(
  props.project?.context || '',
  props.project?.vault_doc_path ? [props.project.vault_doc_path] : [],
))
function openContextFile(event: MouseEvent) {
  const link = (event.target as HTMLElement).closest<HTMLAnchorElement>('a.file-link')
  const path = link?.getAttribute('data-file-path')
  if (!path) return
  event.preventDefault()
  event.stopPropagation()
  emit('open-file', path)
}

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
.agent-context-description {
  margin: 4px 0 0;
  color: var(--fg2);
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
.agent-context-description :deep(a.file-link) {
  color: inherit;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 2px;
}
.agent-context-description :deep(a.file-link:hover) { text-decoration-color: currentColor; }
.agent-context-link:hover,
.agent-context-project:hover { text-decoration-color: currentColor; }
.agent-context-project {
  color: var(--fg);
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
</style>
