<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { writeClipboard } from '../lib/codeCopy'
import { api } from '../lib/api'
import { branchLabel } from '../lib/repo'
import { chatLocationDiffers, repoChip } from '../lib/repoLocation'
import type { ProjectRepo, RepoLocation } from '../lib/types'
import AppIcon from './AppIcon.vue'

const props = defineProps<{
  repo: ProjectRepo
  location: Partial<RepoLocation> | null
  chatId: string
  eligible: boolean
}>()
const copied = ref(false)
const repoLabelId = useId()
let copyTimer: ReturnType<typeof setTimeout> | undefined
const chip = computed(() => repoChip(props.repo, props.location)!)
const differs = computed(() => chatLocationDiffers(props.repo, props.location))
type Changes = { state: 'ready'; source: 'worktree' | 'linked_checkout'; staged: string; unstaged: string; untracked: string[]; truncated: boolean } | { state: 'unavailable'; reason?: string }
const expanded = ref(false)
const loading = ref(false)
const error = ref(false)
const changes = ref<Changes | null>(null)
let requestId = 0

async function loadChanges() {
  const request = ++requestId
  loading.value = true
  error.value = false
  try {
    const result = await api.get<Changes>(`/api/chats/${encodeURIComponent(props.chatId)}/worktree-changes`)
    if (request === requestId) changes.value = result
  } catch {
    if (request === requestId) error.value = true
  } finally {
    if (request === requestId) loading.value = false
  }
}

function toggleChanges() {
  expanded.value = !expanded.value
  if (expanded.value && !changes.value && !loading.value && !error.value) void loadChanges()
}

watch(() => [props.chatId, props.repo.path, props.repo.missing, props.location?.path, props.location?.is_worktree], () => {
  requestId++
  expanded.value = false
  changes.value = null
  error.value = false
  loading.value = false
})

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
onBeforeUnmount(() => { clearTimeout(copyTimer); requestId++ })
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
        <div v-if="eligible" class="cd-row chat-repo-changes-control">
          <button type="button" class="cd-action chat-repo-changes-button" :aria-expanded="expanded" @click="toggleChanges">
            {{ expanded ? 'Hide changes' : 'View changes' }}
          </button>
          <button v-if="expanded && !loading && !error" type="button" class="cd-action chat-repo-changes-button" @click="loadChanges">Refresh</button>
        </div>
        <div v-if="expanded" class="chat-repo-changes" aria-live="polite">
          <p v-if="loading" role="status">Loading checkout changes…</p>
          <div v-else-if="error" role="alert">
            <p>Could not read checkout changes.</p><button type="button" class="cd-action chat-repo-changes-button" @click="loadChanges">Retry</button>
          </div>
          <p v-else-if="changes?.state === 'unavailable'" role="status">{{ changes.reason === 'unsupported_checkout' ? 'This checkout uses Git features that cannot be read safely.' : 'Changes are unavailable for this checkout.' }}</p>
          <template v-else-if="changes?.state === 'ready'">
            <h4>{{ changes.source === 'worktree' ? 'Worktree changes' : 'Linked checkout changes' }}</h4>
            <p v-if="changes.truncated" role="status">Snapshot truncated to keep it within the display limit.</p>
            <p v-if="!changes.staged && !changes.unstaged && !changes.untracked.length" role="status">No uncommitted changes.</p>
            <section v-if="changes.staged"><h5>Staged</h5><pre>{{ changes.staged }}</pre></section>
            <section v-if="changes.unstaged"><h5>Unstaged</h5><pre>{{ changes.unstaged }}</pre></section>
            <section v-if="changes.untracked.length"><h5>Untracked</h5><ul><li v-for="(name, index) in changes.untracked" :key="`${index}:${name}`">{{ name }}</li></ul></section>
          </template>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.chat-repo-kind { color: var(--fg3); }
.chat-repo-dirty { display: inline-flex; align-items: center; gap: 5px; }
.chat-repo-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--warning); flex: none; }
/* Warning colour wins over .cd-detail via the compound selector. */
.cd-detail.chat-repo-missing { color: var(--warning); }
.cd-row.chat-repo-changes-control {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.chat-repo-changes-button { min-height: 44px; }
.chat-repo-changes { max-height: 28rem; overflow: auto; overflow-wrap: anywhere; }
.chat-repo-changes pre { max-height: 18rem; overflow: auto; white-space: pre; font: inherit; }
.chat-repo-changes li { white-space: pre-wrap; overflow-wrap: anywhere; }
.chat-repo-changes h4, .chat-repo-changes h5 { margin: 0.5rem 0; }
</style>
<style scoped src="./chatDetails.css"></style>
