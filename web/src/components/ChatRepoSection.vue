<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { writeClipboard } from '../lib/codeCopy'
import { branchLabel } from '../lib/repo'
import { chatLocationDiffers, repoChip } from '../lib/repoLocation'
import type { ProjectRepo, RepoLocation } from '../lib/types'
import AppIcon from './AppIcon.vue'

const props = defineProps<{
  repo: ProjectRepo
  location: Partial<RepoLocation> | null
  chatId: string
}>()
const copied = ref(false)
const repoLabelId = useId()
let copyTimer: ReturnType<typeof setTimeout> | undefined
const chip = computed(() => repoChip(props.repo, props.location)!)
const differs = computed(() => chatLocationDiffers(props.repo, props.location))

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

watch(() => props.chatId, resetCopied)
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
    </div>
    <p v-if="!chip.missing" class="cd-detail cd-detail--wrap chat-repo-code-note">Chats in this project work in code mode: the repository's instructions, skills and write access are loaded.</p>
  </section>
</template>

<style scoped>
.chat-repo-code-note { margin: var(--space-1) 0 0 26px; }
.chat-repo-kind { color: var(--fg3); }
.chat-repo-dirty { display: inline-flex; align-items: center; gap: 5px; }
.chat-repo-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--warning); flex: none; }
/* Warning colour wins over .cd-detail via the compound selector. */
.cd-detail.chat-repo-missing { color: var(--warning); }
</style>
<style scoped src="./chatDetails.css"></style>
