<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { apiErrorMessage } from '../lib/errorMessage'
import { branchLabel, findRepoInstructions } from '../lib/repo'
import { chatLocationDiffers, repoChip } from '../lib/repoLocation'
import type { ProjectRepo, RepoLocation } from '../lib/types'
import { useProjectStore } from '../stores/projects'
import HostTerminalButton from './HostTerminalButton.vue'

const props = defineProps<{
  repo: ProjectRepo
  location: Partial<RepoLocation> | null
  chatId: string
  codeMode: boolean
  codeModeLocked?: boolean
  archived: boolean
}>()
const emit = defineEmits<{ 'open-file': [path: string] }>()
const store = useProjectStore()
const pending = ref(false)
const error = ref('')
const opening = ref(false)
const openError = ref('')
const switchId = useId()
const hintId = useId()
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

async function openInstructions() {
  opening.value = true
  openError.value = ''
  try {
    const path = await findRepoInstructions(props.repo.path)
    if (path) emit('open-file', path)
    else openError.value = 'This repository has no AGENTS.md or CLAUDE.md yet.'
  } catch (e) {
    openError.value = e instanceof Error ? e.message : String(e)
  } finally {
    opening.value = false
  }
}

watch(() => props.chatId, () => { error.value = ''; openError.value = '' })
</script>

<template>
  <section class="rail-section chat-repo-section" aria-labelledby="chat-rail-repo-label">
    <p id="chat-rail-repo-label" class="rail-label">Repository</p>
    <p class="chat-repo-name">{{ chip.name }}</p>
    <p class="chat-repo-path"><code>{{ repo.path }}</code></p>
    <p v-if="chip.missing" class="chat-repo-missing" role="status">The linked folder is missing. Change or unlink it on the project page.</p>
    <p v-else class="chat-repo-meta">
      <span class="chat-repo-branch"><span aria-hidden="true">⑂ </span><span class="sr-only">branch </span>{{ chip.branch }}</span>
      <span v-if="chip.isWorktree" class="chat-repo-kind">worktree</span>
      <span v-if="chip.dirty === true" class="chat-repo-dirty"><span class="chat-repo-dot" aria-hidden="true"></span>uncommitted changes</span>
      <span v-else-if="chip.dirty === false" class="chat-repo-clean">clean</span>
    </p>
    <p v-if="differs && !chip.missing" class="rail-note chat-repo-note">This chat works in {{ chip.isWorktree ? 'a worktree' : 'another branch' }}: <code>{{ chip.path }}</code>. The main checkout is on ⑂ {{ branchLabel(repo.branch) }}.</p>
    <template v-if="!archived">
      <div class="repo-code-row">
        <span :id="switchId" class="repo-code-label">Code mode</span>
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
      <p :id="hintId" class="repo-code-hint">Loads the repository's instructions, skills and write access in this chat.</p>
      <p v-if="codeModeLocked" :id="lockedHintId" class="repo-code-hint repo-code-locked">Code mode stays on for the rest of this chat.</p>
    </template>
    <p v-if="error" class="repo-code-error" role="alert">{{ error }}</p>
    <button
      v-if="!chip.missing"
      type="button"
      class="chat-repo-open"
      title="Opens AGENTS.md, or CLAUDE.md when the repository has no AGENTS.md"
      :disabled="opening"
      @click="openInstructions"
    >Open AGENTS.md</button>
    <HostTerminalButton v-if="!chip.missing" :chat-id="chatId" class="chat-repo-terminal" />
    <p v-if="openError" class="repo-code-error" role="alert">{{ openError }}</p>
  </section>
</template>

<style scoped>
.repo-code-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-top: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
}
.repo-code-label { font-size: var(--text-sm); }
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
.chat-repo-path, .chat-repo-note code { font-family: var(--font-mono); font-size: var(--text-xs); color: var(--fg2); overflow-wrap: anywhere; }
.chat-repo-path { margin: 2px 0 0; }
.chat-repo-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px var(--space-3); margin: var(--space-1) 0 0; font-size: var(--text-sm); color: var(--fg2); }
.chat-repo-branch { font-family: var(--font-mono); min-width: 0; overflow-wrap: anywhere; }
.chat-repo-kind { color: var(--fg3); }
.chat-repo-dirty { display: inline-flex; align-items: center; gap: 6px; }
.chat-repo-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--warning); flex: none; }
.chat-repo-missing { margin: var(--space-1) 0 0; color: var(--warning); font-size: var(--text-sm); }
.chat-repo-open {
  display: block;
  margin-top: var(--space-2);
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
.chat-repo-terminal { display: block; margin-top: var(--space-2); }
.chat-repo-open:hover { text-decoration-color: currentColor; }
.chat-repo-open:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }
@media (pointer: coarse) {
  .repo-code-switch { min-width: var(--touch); }
  .chat-repo-open { min-height: var(--touch); min-width: var(--touch); }
}
</style>
