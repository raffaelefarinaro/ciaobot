<template>
  <!-- What the agent is given for this chat, each with when it is sent. Shared
       by the Chat details rail and the narrow-pane drawer. -->
  <section v-if="contextPct != null" class="rail-section agent-context" :aria-labelledby="usageLabelId">
    <p :id="usageLabelId" class="rail-label">Context usage</p>
    <div class="agent-context-meter-row">
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
      <button
        type="button"
        class="btn-small agent-context-compact"
        :disabled="compactDisabled"
        :aria-busy="compacting ? 'true' : undefined"
        title="Compact this chat's context into a shorter summary"
        @click="emit('compact')"
      >{{ compacting ? 'Compacting…' : 'Compact' }}</button>
    </div>
    <p class="agent-context-usage-note">{{ contextPctLabel }} of context window used</p>
  </section>
  <!-- Closed by default: these rows are the same every turn, so they are
       reference, not news. The summary still says how much they cost. -->
  <details class="rail-section agent-context agent-context-loaded" :open="loadedOpen" @toggle="onLoadedToggle">
    <summary class="agent-context-summary">
      <svg class="agent-context-chevron" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
      <span class="agent-context-summary-label">Always loaded</span>
      <span v-if="loadedTokens" class="agent-context-meta">{{ formatTokens(loadedTokens) }} tokens</span>
    </summary>
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
      <button v-if="repoInstr" type="button" class="rail-item agent-context-row agent-context-repo"
        :title="`Open ${repoInstrPath}`" @click="emit('open-file', repoInstrPath)">
        <span class="agent-context-row-top">
          <span class="agent-context-name">{{ repoInstrLabel }}</span>
          <span class="agent-context-meta">{{ formatTokens(tokensFor(repoInstr.chars)) }} tokens</span>
        </span>
        <small>Repository instructions</small>
      </button>
      <div v-else-if="repoInstr === null" class="rail-item agent-context-row agent-context-repo" role="status">
        <span class="agent-context-row-top">
          <span class="agent-context-name">Repository instructions</span>
          <span class="agent-context-meta">not found</span>
        </span>
      </div>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import type { ProjectInfo } from '../lib/types'
import { linkifyText } from '../lib/filePaths'
import { joinRepoPath, repoName } from '../lib/repo'
import { fetchWorkspaceGuide, formatTokens, tokensFor, type WorkspaceGuide } from '../lib/workspaceGuide'

const props = defineProps<{
  project: ProjectInfo | null | undefined
  /** Percent of the context window used as of the last reply, when the provider reported it. */
  contextPct: number | null
  /** A compact request is in flight. */
  compacting: boolean
  /** The chat is streaming or a compact request is in flight. */
  compactDisabled: boolean
  /** Code mode is effectively on (chat.code_mode and a linked repo). */
  codeMode?: boolean
  /** Bumped by a caller that points at this section ("See what Ciao knows"): opens Always loaded. */
  revealKey?: number
}>()
const emit = defineEmits<{ 'open-file': [path: string]; compact: [] }>()

// Unique per instance: the rail and the drawer are one component, and the
// usage group's heading must not collide with anything else's.
const usageLabelId = useId()

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

// undefined = no row; null = code mode is on but the repo has no instructions.
const repoInstr = computed(() => (props.codeMode && props.project?.repo) ? (props.project.repo.instructions ?? null) : undefined)
const repoInstrPath = computed(() => repoInstr.value && props.project?.repo ? joinRepoPath(props.project.repo.path, repoInstr.value.name) : '')
const repoInstrLabel = computed(() => repoInstr.value && props.project?.repo ? `${repoName(props.project.repo.path)}/${repoInstr.value.name}` : '')

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

// Open or closed is remembered per browser, like Memory's "How memory works".
const LOADED_OPEN_KEY = 'ciao.chatDetails.alwaysLoadedOpen'
function readLoadedOpen(): boolean {
  try { return localStorage.getItem(LOADED_OPEN_KEY) === '1' } catch { return false }
}
const loadedOpen = ref(readLoadedOpen())
function onLoadedToggle(event: Event) {
  loadedOpen.value = (event.target as HTMLDetailsElement).open
  try { localStorage.setItem(LOADED_OPEN_KEY, loadedOpen.value ? '1' : '0') } catch { /* private mode */ }
}
watch(() => props.revealKey, (key) => { if (key) loadedOpen.value = true })

const loadedTokens = computed(() => {
  let total = guide.value.path ? guideTokens.value : 0
  if (props.project && briefText.value) total += briefTokens.value
  if (repoInstr.value) total += tokensFor(repoInstr.value.chars)
  return total
})

const contextPctLabel = computed(() => {
  const pct = props.contextPct
  if (pct == null) return ''
  return `${pct < 10 ? Math.round(pct * 10) / 10 : Math.round(pct)}%`
})

</script>

<style scoped>
.agent-context-usage-note { margin: 0 0 var(--space-2); color: var(--fg3); font-size: var(--text-xs); }
.agent-context-meter-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin: 4px 0 6px;
}
.agent-context-meter {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.agent-context-compact { flex: none; white-space: nowrap; }
@media (pointer: coarse) {
  .agent-context-compact { min-height: 44px; min-width: 44px; }
  /* A flex item, so line-height gives the 44px box and the ellipsis still works
     (inline-flex would put the text in an anonymous item that cannot truncate). */
  a.agent-context-name { line-height: var(--touch); }
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
.agent-context-summary {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 32px;
  margin: 0 -4px;
  padding: 0 4px;
  border-radius: var(--radius-xs);
  color: var(--fg3);
  font-size: var(--text-sm);
  list-style: none;
  cursor: pointer;
}
.agent-context-summary::-webkit-details-marker { display: none; }
.agent-context-summary:hover { color: var(--fg); }
.agent-context-summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.agent-context-summary-label { flex: 1; min-width: 0; }
.agent-context-chevron { flex: none; transition: transform 140ms cubic-bezier(0.22, 1, 0.36, 1); }
.agent-context-loaded[open] .agent-context-chevron { transform: rotate(90deg); }
.agent-context-loaded[open] .agent-context-summary { margin-bottom: 4px; }
@media (prefers-reduced-motion: reduce) {
  .agent-context-chevron { transition: none; }
}
@media (pointer: coarse) {
  .agent-context-summary { min-height: var(--touch); }
}
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
.agent-context-repo[role='status'] { cursor: default; }
.agent-context-repo[role='status']:hover { color: var(--fg); }
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
@media (pointer: coarse) {
  /* Below the base .agent-context-link rule it overrides. */
  .agent-context-link { min-height: var(--touch); min-width: var(--touch); }
}
.agent-context-project {
  color: var(--fg);
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
</style>
