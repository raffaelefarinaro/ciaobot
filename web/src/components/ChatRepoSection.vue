<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { apiErrorMessage } from '../lib/errorMessage'
import { writeClipboard } from '../lib/codeCopy'
import { branchLabel } from '../lib/repo'
import { chatLocationDiffers, repoChip } from '../lib/repoLocation'
import type { ProjectRepo, RepoLocation } from '../lib/types'
import { useProjectStore } from '../stores/projects'
import AppIcon from './AppIcon.vue'

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
const repoLabelId = useId()
let copyTimer: ReturnType<typeof setTimeout> | undefined
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

watch(() => props.chatId, () => { error.value = ''; resetCopied() })
onBeforeUnmount(() => clearTimeout(copyTimer))
</script>

<template>
  <section class="cd-section chat-repo-section" :aria-labelledby="repoLabelId">
    <div class="cd-head"><h3 :id="repoLabelId" class="cd-heading">Repository</h3></div>
    <div class="cd-rows">
      <div v-if="chip.missing" class="cd-row chat-repo-row chat-repo-missing-row">
        <AppIcon class="cd-icon" name="warning" />
        <span class="cd-main">
          <span class="cd-primary chat-repo-name">{{ chip.name }}</span>
          <span class="cd-detail cd-detail--wrap chat-repo-missing" role="status">The linked folder is missing. Change or unlink it on the project page.</span>
        </span>
      </div>
      <template v-else>
        <div class="cd-row chat-repo-row">
          <AppIcon class="cd-icon" name="folder" />
          <span class="cd-main">
            <span class="cd-primary chat-repo-name">{{ chip.name }}</span>
            <span class="cd-detail cd-mono chat-repo-path" :title="chip.path">{{ chip.path }}</span>
          </span>
          <span class="cd-trail">
            <button
              type="button"
              class="cd-action cd-action--icon chat-repo-copy"
              :aria-label="copied ? 'Path copied' : 'Copy path'"
              :title="chip.path"
              @click="copyPath"
            ><AppIcon :name="copied ? 'check' : 'copy'" /></button>
            <span class="sr-only" aria-live="polite">{{ copied ? 'Path copied' : '' }}</span>
          </span>
        </div>
        <div class="cd-row chat-repo-branch-row">
          <AppIcon class="cd-icon" name="branch" />
          <span class="cd-main">
            <span class="cd-primary cd-mono chat-repo-branch" :title="differs ? `Main checkout is on ${branchLabel(repo.branch)}` : undefined"><span class="sr-only">branch </span>{{ chip.branch }}</span>
            <span class="cd-detail chat-repo-facts">
              <span v-if="chip.isWorktree" class="chat-repo-kind">worktree</span>
              <template v-if="chip.isWorktree && chip.dirty != null"> · </template>
              <span v-if="chip.dirty === true" class="chat-repo-dirty"><span class="chat-repo-dot" aria-hidden="true"></span>uncommitted changes</span>
              <span v-else-if="chip.dirty === false" class="chat-repo-clean">clean</span>
            </span>
          </span>
        </div>
      </template>
      <div v-if="!archived" class="cd-row repo-code-row">
        <AppIcon class="cd-icon" name="code" />
        <span class="cd-main">
          <span :id="switchId" class="cd-primary repo-code-label">Code mode</span>
          <span v-if="codeModeLocked" class="cd-detail repo-code-lock-note">Locked on for this chat</span>
          <span :id="hintId" class="cd-detail cd-detail--wrap repo-code-hint" :class="{ 'sr-only': codeModeLocked }">Loads the repository's instructions, skills and write access in this chat.</span>
        </span>
        <span class="cd-trail">
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
        </span>
      </div>
    </div>
    <p v-if="!archived && codeModeLocked" :id="lockedHintId" class="sr-only repo-code-locked">Code mode stays on for the rest of this chat.</p>
    <p v-if="error" class="repo-code-error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.repo-code-error { margin: var(--space-1) 0 0 26px; color: var(--error); font-size: var(--text-sm); }
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
.chat-repo-kind { color: var(--fg3); }
.chat-repo-dirty { display: inline-flex; align-items: center; gap: 5px; }
.chat-repo-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--warning); flex: none; }
/* Warning colour wins over .cd-detail via the compound selector. */
.cd-detail.chat-repo-missing { color: var(--warning); }
@media (pointer: coarse) {
  .repo-code-switch { min-width: var(--touch); }
}
</style>
<style scoped src="./chatDetails.css"></style>
