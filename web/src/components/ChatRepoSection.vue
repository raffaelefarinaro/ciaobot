<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { apiErrorMessage } from '../lib/errorMessage'
import { writeClipboard } from '../lib/codeCopy'
import { branchLabel } from '../lib/repo'
import { chatLocationDiffers, repoChip } from '../lib/repoLocation'
import type { ProjectRepo, RepoLocation } from '../lib/types'
import { useProjectStore } from '../stores/projects'

const props = defineProps<{
  repo: ProjectRepo
  location: Partial<RepoLocation> | null
  chatId: string
  codeMode: boolean
  codeModeLocked?: boolean
  archived: boolean
}>()
const store = useProjectStore()
const pending = ref(false)
const error = ref('')
const copied = ref(false)
const infoOpen = ref(false)
const branchOpen = ref(false)
const branchBodyId = useId()
let copyTimer: ReturnType<typeof setTimeout> | undefined
const switchId = useId()
const hintId = useId()
const hintWrapId = useId()
const lockedHintId = useId()
const chip = computed(() => repoChip(props.repo, props.location)!)
const differs = computed(() => chatLocationDiffers(props.repo, props.location))

async function toggleCodeMode() {
  if (pending.value || props.codeModeLocked) return
  pending.value = true
  error.value = ''
  try {
    await store.setChatCodeMode(props.chatId, !props.codeMode)
  } catch (e) {
    error.value = apiErrorMessage(e, 'Could not change code mode.')
  } finally {
    pending.value = false
  }
}

async function copyPath() {
  if (!(await writeClipboard(chip.value.path))) return
  copied.value = true
  clearTimeout(copyTimer)
  copyTimer = setTimeout(() => { copied.value = false }, 1500)
}

function resetCopied() {
  clearTimeout(copyTimer)
  copied.value = false
}

watch(() => props.chatId, () => { error.value = ''; branchOpen.value = false; resetCopied() })
onBeforeUnmount(() => clearTimeout(copyTimer))
</script>

<template>
  <section class="rail-section chat-repo-section" aria-labelledby="chat-rail-repo-label">
    <p id="chat-rail-repo-label" class="rail-label">Repository</p>
    <div class="chat-repo-head">
      <p class="chat-repo-name" :title="chip.path">{{ chip.name }}</p>
      <button
        v-if="!chip.missing"
        type="button"
        class="chat-repo-copy"
        :aria-label="copied ? 'Path copied' : 'Copy path'"
        :title="chip.path"
        @click="copyPath"
      >
        <svg v-if="!copied" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" /><path d="M10.5 5.5v-2a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 3.5V9A1.5 1.5 0 0 0 4 10.5h1.5" /></svg>
        <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8.5l3.2 3.2L13 4.8" /></svg>
      </button>
      <span class="sr-only" aria-live="polite">{{ copied ? 'Path copied' : '' }}</span>
    </div>
    <p v-if="chip.missing" class="chat-repo-missing" role="status">The linked folder is missing. Change or unlink it on the project page.</p>
    <div v-else class="chat-repo-meta">
      <!-- The branch stays one line, with the facts about the checkout beside
           it. Its details open underneath at the section's full width, so the
           long path wraps across the rail instead of down a sliver beside the
           facts. -->
      <div class="chat-repo-branch-line">
        <button
          type="button"
          class="chat-repo-branch"
          :aria-expanded="branchOpen"
          :aria-controls="branchBodyId"
          :title="differs ? `Main checkout is on ${branchLabel(repo.branch)}` : undefined"
          @click="branchOpen = !branchOpen"
        >
          <svg class="chat-repo-chevron" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
          <svg class="chat-repo-branch-icon" width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="4.5" cy="3.5" r="1.5" /><circle cx="4.5" cy="12.5" r="1.5" /><circle cx="11.5" cy="5.5" r="1.5" /><path d="M4.5 5v6M11.5 7c0 2.5-2 3.5-5.5 4" /></svg>
          <span class="sr-only">branch </span><span class="chat-repo-branch-name">{{ chip.branch }}</span>
        </button>
        <span class="chat-repo-facts">
          <span v-if="chip.isWorktree" class="chat-repo-kind">worktree</span>
          <span v-if="chip.dirty === true" class="chat-repo-dirty"><span class="chat-repo-dot" aria-hidden="true"></span>uncommitted changes</span>
          <span v-else-if="chip.dirty === false" class="chat-repo-clean">clean</span>
        </span>
      </div>
      <dl v-show="branchOpen" :id="branchBodyId" class="chat-repo-branch-body">
        <dt>Branch</dt>
        <dd class="chat-repo-branch-value">{{ chip.branch }}</dd>
        <dt>Path</dt>
        <dd class="chat-repo-path-value">{{ chip.path }}</dd>
      </dl>
    </div>
    <template v-if="!archived">
      <div class="repo-code-row">
        <span class="repo-code-label-block">
          <span :id="switchId" class="repo-code-label">Code mode</span>
          <span v-if="codeModeLocked" class="repo-code-lock-note">Locked on for this chat</span>
        </span>
        <button
          type="button"
          class="repo-code-info"
          aria-label="About code mode"
          :aria-expanded="infoOpen"
          :aria-controls="hintWrapId"
          @click="infoOpen = !infoOpen"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6" /><path d="M8 7.2v3.6" /><circle cx="8" cy="5" r="0.4" fill="currentColor" /></svg>
        </button>
        <button
          type="button"
          role="switch"
          class="repo-code-switch"
          :aria-checked="codeMode"
          :aria-labelledby="switchId"
          :aria-busy="pending"
          :aria-disabled="pending || codeModeLocked"
          :aria-describedby="codeModeLocked ? `${hintId} ${lockedHintId}` : hintId"
          @click="toggleCodeMode"
        >
          <span class="switch-word">{{ codeMode ? 'On' : 'Off' }}</span>
          <span class="switch-track" aria-hidden="true"></span>
        </button>
      </div>
      <div :id="hintWrapId" :class="{ 'sr-only': !infoOpen }">
        <p :id="hintId" class="repo-code-hint">Loads the repository's instructions, skills and write access in this chat.</p>
        <p v-if="codeModeLocked" :id="lockedHintId" class="repo-code-hint repo-code-locked">Code mode stays on for the rest of this chat.</p>
      </div>
    </template>
    <p v-if="error" class="repo-code-error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.repo-code-row {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
}
.repo-code-label-block { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.repo-code-label { font-size: var(--text-sm); }
.repo-code-row .repo-code-switch { margin-left: auto; }
.repo-code-lock-note { color: var(--fg3); font-size: var(--text-xs); }
.repo-code-hint { margin: var(--space-1) 0 0; color: var(--fg3); font-size: var(--text-xs); }
.repo-code-error { margin: var(--space-2) 0 0; color: var(--error); font-size: var(--text-sm); }
/* A switch reads as state; the visible On/Off word means it does not rely on
   colour. Copied from settings/settingsPanels.css, which is scoped. */
.repo-code-switch {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  padding: 0 2px;
  border: 0;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.repo-code-switch[aria-disabled='true'] { opacity: 0.6; cursor: default; }
.repo-code-switch:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
.repo-code-switch .switch-word { min-width: 2.2em; text-align: right; }
.repo-code-switch .switch-track {
  position: relative;
  width: 36px;
  height: 20px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-pill);
  background: var(--bg3);
  transition: background 120ms ease, border-color 120ms ease;
}
.repo-code-switch .switch-track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--fg3);
  transition: transform 120ms ease, background 120ms ease;
}
.repo-code-switch[aria-checked='true'] .switch-word { color: var(--fg); }
.repo-code-switch[aria-checked='true'] .switch-track {
  border-color: transparent;
  background: var(--accent);
}
.repo-code-switch[aria-checked='true'] .switch-track::after {
  transform: translateX(16px);
  background: var(--on-accent);
}
@media (prefers-reduced-motion: reduce) {
  .repo-code-switch .switch-track,
  .repo-code-switch .switch-track::after { transition: none; }
}
.chat-repo-name { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
.chat-repo-head { display: flex; align-items: baseline; gap: var(--space-2); }
.chat-repo-copy, .repo-code-info {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  align-self: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--fg3);
  cursor: pointer;
}
.chat-repo-copy:hover, .repo-code-info:hover { color: var(--fg); }
.chat-repo-copy:focus-visible, .repo-code-info:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }
.chat-repo-meta { margin: var(--space-1) 0 0; font-size: var(--text-sm); color: var(--fg2); }
/* The facts sit beside the branch when both fit and drop under it when they
   do not; a name wider than the rail ellipsizes. */
.chat-repo-branch-line { display: flex; flex-wrap: wrap; align-items: center; gap: 2px var(--space-3); min-width: 0; }
.chat-repo-facts { display: inline-flex; flex-wrap: wrap; flex: none; align-items: center; gap: 2px var(--space-3); }
.chat-repo-branch {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
  margin-left: -2px;
  padding: 2px;
  border: 0;
  border-radius: var(--radius-xs);
  background: none;
  color: inherit;
  font: inherit;
  cursor: pointer;
}
.chat-repo-branch-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); }
.chat-repo-chevron, .chat-repo-branch-icon { flex: none; color: var(--fg3); }
.chat-repo-chevron { transition: transform 140ms cubic-bezier(0.22, 1, 0.36, 1); }
.chat-repo-branch[aria-expanded='true'] .chat-repo-chevron { transform: rotate(90deg); }
.chat-repo-branch:hover, .chat-repo-branch:hover .chat-repo-chevron { color: var(--fg); }
.chat-repo-branch:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .chat-repo-chevron { transition: none; } }
/* The details sit under the line at the section's full width, as an inset
   key/value block, so the path wraps across the rail. */
.chat-repo-branch-body {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 4px var(--space-3);
  margin: var(--space-2) 0 0;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--bg3);
}
.chat-repo-branch-body dt { color: var(--fg3); font-size: var(--text-xs); line-height: 1.6; }
.chat-repo-branch-body dd { margin: 0; }
.chat-repo-branch-value, .chat-repo-path-value { font-family: var(--font-mono); font-size: var(--text-xs); line-height: 1.6; color: var(--fg); overflow-wrap: anywhere; }
.chat-repo-kind { color: var(--fg3); }
.chat-repo-dirty { display: inline-flex; align-items: center; gap: 6px; }
.chat-repo-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--warning); flex: none; }
.chat-repo-missing { margin: var(--space-1) 0 0; color: var(--warning); font-size: var(--text-sm); }
@media (pointer: coarse) {
  .repo-code-switch { min-width: var(--touch); }
  .chat-repo-copy, .repo-code-info { width: var(--touch); height: var(--touch); }
  /* The branch toggle is a tap target like any other control: 44px in both
     axes, its one line centred in that box. */
  .chat-repo-branch { min-height: var(--touch); min-width: var(--touch); }
}
</style>
