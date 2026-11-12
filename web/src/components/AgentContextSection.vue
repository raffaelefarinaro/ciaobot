<template>
  <!-- What the agent is given for this chat: how full the context window is,
       and what is sent with every turn. Shared by the rail and the drawer. -->
  <section class="cd-section agent-context" :aria-labelledby="usageLabelId">
    <div class="cd-head">
      <h3 :id="usageLabelId" class="cd-heading">Context</h3>
      <button
        v-if="contextPct != null || compacted"
        type="button"
        class="cd-action cd-head-action agent-context-compact"
        :disabled="compactDisabled"
        :aria-busy="compacting ? 'true' : undefined"
        title="Compact this chat's context into a shorter summary"
        @click="emit('compact')"
      >{{ compacting ? 'Compacting…' : 'Compact' }}</button>
    </div>
    <div
      v-if="contextPct != null"
      class="agent-context-meter"
      role="meter"
      :aria-valuenow="contextPct"
      aria-valuemin="0"
      aria-valuemax="100"
      aria-label="Context window used"
    >
      <span class="agent-context-track"><span class="agent-context-fill" :style="{ width: `${Math.min(100, contextPct)}%` }"></span></span>
      <span class="agent-context-pct">{{ contextPctLabel }} used</span>
    </div>
    <!-- Closed by default: these rows are the same every turn, so they are
         reference, not news. The summary still says how much they cost. -->
    <details class="agent-context-loaded" :open="loadedOpen" @toggle="onLoadedToggle">
      <summary class="cd-row cd-row--link agent-context-summary">
        <AppIcon class="cd-icon agent-context-chevron" name="chevron" />
        <span class="cd-main"><span class="cd-primary agent-context-summary-label">Always loaded</span></span>
        <span v-if="loadedTokens && totalComplete" class="cd-trail cd-value agent-context-meta">{{ formatTokens(loadedTokens) }} tokens</span>
      </summary>
      <div class="cd-rows cd-nested agent-context-rows">
        <div v-if="systemPrompt" class="agent-context-row agent-context-system">
          <button
            type="button"
            class="cd-row cd-row--link agent-context-system-toggle"
            :aria-expanded="systemPromptOpen"
            :aria-controls="systemPromptOpen ? systemPromptId : undefined"
            @click="systemPromptOpen = !systemPromptOpen"
          >
            <AppIcon class="cd-icon" name="spark" />
            <span class="cd-main">
              <span class="cd-primary agent-context-name">Ciaobot system prompt</span>
              <span class="cd-detail">Added to the provider's own system prompt · every session</span>
            </span>
            <span class="cd-trail cd-value agent-context-meta">{{ formatTokens(systemPromptTokens) }} tokens</span>
          </button>
          <div v-if="systemPromptOpen" :id="systemPromptId" class="agent-context-system-body">
            <button type="button" class="cd-action agent-context-copy" aria-live="polite" @click="copySystemPrompt">{{ copied ? 'Copied' : 'Copy' }}</button>
            <pre class="agent-context-system-text" tabindex="0">{{ systemPrompt }}</pre>
          </div>
        </div>
        <div v-else-if="systemPromptState === 'failed'" class="cd-row agent-context-row agent-context-error agent-context-system-error" role="status">
          <AppIcon class="cd-icon" name="warning" />
          <span class="cd-main">
            <span class="cd-primary">Ciaobot system prompt</span>
            <span class="cd-detail cd-detail--wrap agent-context-error-text">Could not be read just now</span>
          </span>
          <span class="cd-trail"><button type="button" class="cd-action" @click="retryNow(loadSystemPrompt)">Retry</button></span>
        </div>
        <button
          v-if="guide.path"
          type="button"
          class="cd-row cd-row--link agent-context-row"
          :title="`Open ${guide.path}`"
          @click="emit('open-file', guide.path)"
        >
          <AppIcon class="cd-icon" name="doc" />
          <span class="cd-main">
            <span class="cd-primary agent-context-name">{{ guideName }}</span>
            <span class="cd-detail">Workspace guide · read every turn</span>
          </span>
          <span class="cd-trail cd-value agent-context-meta">{{ formatTokens(guideTokens) }} tokens</span>
        </button>
        <div v-else-if="guide.error" class="cd-row agent-context-row agent-context-error" role="status">
          <AppIcon class="cd-icon" name="warning" />
          <span class="cd-main">
            <span class="cd-primary">Workspace guide</span>
            <span class="cd-detail cd-detail--wrap agent-context-error-text">Could not be read just now: {{ guide.error }}</span>
          </span>
          <span class="cd-trail"><button type="button" class="cd-action" @click="retryNow(loadGuide)">Retry</button></span>
        </div>
        <!-- General with no description or doc sends no brief at all. -->
        <div v-if="project && briefText" class="cd-row agent-context-row agent-context-brief">
          <AppIcon class="cd-icon" name="folder" />
          <span class="cd-main">
            <router-link :to="`/project/${project.project_id}`" class="cd-primary agent-context-name agent-context-project">{{ project.name }}</router-link>
            <p
              v-if="project.context"
              class="agent-context-description"
              @click="openContextFile"
              v-html="contextHtml"
            ></p>
          </span>
          <span class="cd-trail cd-value agent-context-meta">{{ formatTokens(briefTokens) }} tokens</span>
        </div>
        <button
          v-if="repoInstr"
          type="button"
          class="cd-row cd-row--link agent-context-row agent-context-repo"
          :title="`Open ${repoInstrPath}`"
          @click="emit('open-file', repoInstrPath)"
        >
          <AppIcon class="cd-icon" name="code" />
          <span class="cd-main">
            <span class="cd-primary agent-context-name">{{ repoInstrLabel }}</span>
            <span class="cd-detail">Repository instructions</span>
          </span>
          <span class="cd-trail cd-value agent-context-meta">{{ formatTokens(tokensFor(repoInstr.chars)) }} tokens</span>
        </button>
        <div v-else-if="repoInstr === null" class="cd-row agent-context-row agent-context-repo" role="status">
          <AppIcon class="cd-icon" name="code" />
          <span class="cd-main"><span class="cd-primary">Repository instructions</span></span>
          <span class="cd-trail cd-value agent-context-meta">not found</span>
        </div>
      </div>
    </details>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import type { ProjectInfo } from '../lib/types'
import { linkifyText } from '../lib/filePaths'
import { inCodeMode, joinRepoPath, repoName } from '../lib/repo'
import { writeClipboard } from '../lib/codeCopy'
import AppIcon from './AppIcon.vue'
import { fetchSystemPrompt } from '../lib/systemPrompt'
import { fetchWorkspaceGuide, formatTokens, tokensFor, type WorkspaceGuide } from '../lib/workspaceGuide'

const props = defineProps<{
  project: ProjectInfo | null | undefined
  /** Percent of the context window used as of the last reply, when the provider reported it. */
  contextPct: number | null
  /** A compaction is newer than the last reported usage, so the old figure no longer holds. */
  compacted?: boolean
  /** A compact request is in flight. */
  compacting: boolean
  /** The chat is streaming or a compact request is in flight. */
  compactDisabled: boolean
  /** Bumped by a caller that points at this section ("See what Ciao knows"): opens Always loaded. */
  revealKey?: number
}>()
const emit = defineEmits<{ 'open-file': [path: string]; compact: [] }>()

// Unique per instance: the rail and the drawer are one component, and the
// Context heading must not collide with anything else's.
const usageLabelId = useId()

const guide = ref<WorkspaceGuide>({ path: '', content: '', error: '' })
const guideLoading = ref(true)
let guideSeq = 0
// A failed read (network, non-404) keeps its error so the row says the guide
// is unreadable rather than disappearing as if the workspace had none.
async function loadGuide() {
  const workspace = props.project?.workspace
  const seq = ++guideSeq
  clearRetryTimer()
  if (!workspace) { guide.value = { path: '', content: '', error: '' }; guideLoading.value = false; settle(); return }
  guideLoading.value = true
  const result = await fetchWorkspaceGuide(workspace)
  if (seq !== guideSeq) return
  guide.value = result
  guideLoading.value = false
  settle()
}

// A read that fails while the engine is cold (right after a restart) retries on
// its own with a bounded backoff. One timer re-runs whichever parts failed.
const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000]
let retryTimer: ReturnType<typeof setTimeout> | undefined
let retryAttempt = 0
let disposed = false
function clearRetryTimer() { clearTimeout(retryTimer); retryTimer = undefined }
const guideFailed = computed(() => !guideLoading.value && !guide.value.path && !!guide.value.error)
function settle() {
  if (disposed || retryTimer) return
  if (!guideFailed.value && systemPromptState.value !== 'failed') return
  if (retryAttempt >= RETRY_DELAYS_MS.length) return
  retryTimer = setTimeout(() => {
    retryTimer = undefined
    if (systemPromptState.value === 'failed') void loadSystemPrompt()
    if (guideFailed.value) void loadGuide()
  }, RETRY_DELAYS_MS[retryAttempt++])
}
function retryNow(load: () => Promise<void>) { retryAttempt = 0; void load() }
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

// undefined = no row (not in code mode); null = code mode, but the repo has no instructions.
const repoInstr = computed(() => inCodeMode(props.project?.repo) ? (props.project.repo.instructions ?? null) : undefined)
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

// Ciaobot's own prompt is static per engine. A failed read shows an error row
// and retries; it is not treated as "no prompt".
const systemPrompt = ref('')
const systemPromptState = ref<'loading' | 'ok' | 'failed'>('loading')
const systemPromptOpen = ref(false)
const systemPromptId = useId()
const systemPromptTokens = computed(() => tokensFor(systemPrompt.value.length))
let promptSeq = 0
async function loadSystemPrompt() {
  const seq = ++promptSeq
  clearRetryTimer()
  const text = await fetchSystemPrompt()
  if (seq !== promptSeq) return
  systemPrompt.value = text ?? ''
  systemPromptState.value = text === null ? 'failed' : 'ok'
  settle()
}
onMounted(loadSystemPrompt)
watch(() => props.project?.workspace, () => { retryAttempt = 0; void loadGuide() }, { immediate: true })
const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | undefined
async function copySystemPrompt() {
  if (!(await writeClipboard(systemPrompt.value))) return
  copied.value = true
  clearTimeout(copyTimer)
  copyTimer = setTimeout(() => { copied.value = false }, 1500)
}
onBeforeUnmount(() => { clearTimeout(copyTimer); clearRetryTimer(); disposed = true })

// Never show a partial number as if it were the whole.
const totalComplete = computed(() => systemPromptState.value === 'ok' && !guideLoading.value && !guide.value.error)
const loadedTokens = computed(() => {
  let total = guide.value.path ? guideTokens.value : 0
  if (systemPrompt.value) total += systemPromptTokens.value
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
.agent-context-meter { display: flex; align-items: center; gap: var(--space-3); min-height: 32px; }
@media (pointer: coarse) {
  /* A flex item, so line-height gives the 44px box and the ellipsis still works
     (inline-flex would put the text in an anonymous item that cannot truncate). */
  a.agent-context-name { line-height: var(--touch); }
}
.agent-context-track {
  flex: 1;
  min-width: 0;
  height: 4px;
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
.agent-context-pct { flex: none; min-width: 6ch; color: var(--fg2); font-variant-numeric: tabular-nums; text-align: right; }
.agent-context-summary { list-style: none; }
.agent-context-summary::-webkit-details-marker { display: none; }
.agent-context-chevron { transition: transform 140ms cubic-bezier(0.22, 1, 0.36, 1); }
.agent-context-loaded[open] .agent-context-chevron { transform: rotate(90deg); }
@media (prefers-reduced-motion: reduce) {
  .agent-context-chevron { transition: none; }
}
.cd-detail.agent-context-error-text { color: var(--warning); }
.agent-context-system-body { display: flex; flex-direction: column; align-items: flex-end; margin-top: 4px; padding-left: 26px; }
.agent-context-system-text {
  align-self: stretch;
  max-height: 320px;
  overflow: auto;
  margin: 6px 0 0;
  padding: 8px;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font: calc(11px * var(--font-scale)) / 1.5 var(--font-mono);
  color: var(--fg2);
}
.agent-context-description {
  margin: 2px 0 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.agent-context-description :deep(a.file-link) {
  color: inherit;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 2px;
}
.agent-context-description :deep(a.file-link:hover) { text-decoration-color: currentColor; }
.cd-primary.agent-context-project { color: var(--fg); text-decoration: none; }
.cd-primary.agent-context-project:hover { text-decoration: underline; }
</style>
<style scoped src="./chatDetails.css"></style>
