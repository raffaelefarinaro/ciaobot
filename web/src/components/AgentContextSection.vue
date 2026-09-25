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
      <div v-if="project" class="rail-item agent-context-row agent-context-brief">
        <span class="agent-context-row-top">
          <router-link :to="`/project/${project.project_id}`" class="agent-context-name agent-context-project">{{ project.name }}</router-link>
          <span class="agent-context-meta">{{ formatTokens(briefTokens) }} tokens</span>
        </span>
        <small>
          Project brief: a short description<template v-if="project.vault_doc_path"> and a link to
            <button type="button" class="agent-context-link" :title="`Open ${project.vault_doc_path}`" @click="emit('open-file', project.vault_doc_path)">{{ docName }}</button>,
            which the agent opens when it needs it</template>. Sent at the start and when it changes.
        </small>
        <small v-if="project.context" class="agent-context-quote">“{{ project.context }}”</small>
      </div>
    </div>
  </section>
  <section class="rail-section" aria-labelledby="agent-context-notes-label">
    <p id="agent-context-notes-label" class="rail-label">Notes matched in your last message</p>
    <div v-if="entities.length" class="rail-list">
      <button
        v-for="entity in entities"
        :key="entity.path"
        type="button"
        class="rail-item agent-context-row"
        :disabled="!resolved.get(entity.path)"
        :title="resolved.get(entity.path) ? `Open ${resolved.get(entity.path)}` : entity.path"
        @click="openEntity(entity)"
      >
        <span class="agent-context-row-top">
          <span class="agent-context-name">{{ entity.name }}</span>
          <span class="agent-context-meta">{{ entity.category }}</span>
        </span>
      </button>
    </div>
    <p v-else class="rail-note agent-context-empty">{{ hasUserMessage ? 'None.' : 'Send a message to see which notes it matches.' }}</p>
    <p class="rail-note">Sent as links only. The agent opens a note when it needs it.</p>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ContextEntity, ProjectInfo } from '../lib/types'
import { fetchWorkspaceGuide, formatTokens, tokensFor, type WorkspaceGuide } from '../lib/workspaceGuide'
import { buildMarkdownIndex, resolveVaultLinkTarget } from '../lib/vaultLinks'
import { useFileViewerStore } from '../stores/fileViewer'

const props = defineProps<{
  project: ProjectInfo | null | undefined
  /** The latest user message's matches; undefined when there is no user message yet. */
  entities: ContextEntity[] | undefined
  /** Percent of the context window used as of the last reply, when the provider reported it. */
  contextPct: number | null
}>()
const emit = defineEmits<{ 'open-file': [path: string] }>()

const fileViewer = useFileViewerStore()

const guide = ref<WorkspaceGuide>({ path: '', content: '', error: '' })
let guideSeq = 0
watch(() => props.project?.workspace, async (workspace) => {
  const seq = ++guideSeq
  if (!workspace) { guide.value = { path: '', content: '', error: '' }; return }
  const result = await fetchWorkspaceGuide(workspace)
  if (seq === guideSeq) guide.value = result
}, { immediate: true })
const guideName = computed(() => guide.value.path.split('/').pop() || 'AGENTS.md')
const guideTokens = computed(() => tokensFor(guide.value.content.length))

// The brief is the capsule's stable project lines (ciao/context/capsule.py):
// the name (General is implicit), the description and the canonical doc's
// path. Not the doc itself.
const briefTokens = computed(() => {
  const p = props.project
  if (!p) return 0
  const lines = [
    p.name && p.name !== 'General' ? `project="${p.name}"` : '',
    p.context ? `project_context=${p.context}` : '',
    p.vault_doc_path ? `canonical_doc=${p.vault_doc_path}` : '',
  ].filter(Boolean)
  return tokensFor(lines.join('\n').length)
})
const docName = computed(() => props.project?.vault_doc_path?.split('/').pop() || '')

const contextPctLabel = computed(() => {
  const pct = props.contextPct
  if (pct == null) return ''
  return `${pct < 10 ? Math.round(pct * 10) / 10 : Math.round(pct)}%`
})

const hasUserMessage = computed(() => props.entities !== undefined)
const entities = computed(() => props.entities ?? [])

// Hints are vault-root-relative; the viewer wants workspace-relative paths.
// Resolve them against the vault's markdown path list, the same way an
// in-note link resolves.
const markdownPaths = ref<string[]>([])
watch(() => entities.value.length > 0, async (any) => {
  if (!any || markdownPaths.value.length) return
  markdownPaths.value = fileViewer.markdownPaths.length ? fileViewer.markdownPaths : await fileViewer.loadMarkdownPaths()
}, { immediate: true })
const resolved = computed(() => {
  const out = new Map<string, string>()
  if (!markdownPaths.value.length) return out
  const index = buildMarkdownIndex(markdownPaths.value)
  const set = new Set(markdownPaths.value)
  for (const entity of entities.value) {
    const target = resolveVaultLinkTarget(entity.path, '', index, set)
    if (target) out.set(entity.path, target)
  }
  return out
})
function openEntity(entity: ContextEntity): void {
  const target = resolved.value.get(entity.path)
  if (target) emit('open-file', target)
}
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
.agent-context-quote {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
  margin-top: 2px;
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
.agent-context-link:hover,
.agent-context-project:hover { text-decoration-color: currentColor; }
.agent-context-project {
  color: var(--fg);
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
.agent-context-row:disabled { cursor: default; color: var(--fg2); }
.agent-context-row:disabled:hover { color: var(--fg2); }
.agent-context-empty { margin-top: 0; }
</style>
