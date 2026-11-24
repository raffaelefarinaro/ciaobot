<template>
  <div class="card">
    <div class="settings-card-header">
      <p class="section-title">Shared brains</p>
      <p class="hint">{{ SHARED_BRAIN_PRIVACY }}</p>
    </div>

    <!-- First load. A pending read is its own state, never an empty claim. -->
    <div v-if="!brains && !loadError" class="hint" role="status">Checking the shared brains&hellip;</div>

    <!-- First-load failure: the error and a retry, never a guessed list. -->
    <template v-else-if="!brains">
      <p class="action-result action-result--error" role="alert">
        Could not read the shared brains: {{ loadError }}
      </p>
      <div class="action-row settings-actions">
        <button type="button" class="btn-secondary btn-small" :disabled="loading" @click="load()">
          {{ loading ? 'Checking...' : 'Try again' }}
        </button>
      </div>
    </template>

    <template v-else>
      <div v-if="listStale" class="hint hint--warn" role="alert">
        <p class="shared-brain-stale">
          Could not refresh the list: {{ loadError }}. Showing the last known list. Adding and removing wait until it is read again.
        </p>
        <button type="button" class="btn-secondary btn-small" :disabled="loading" @click="load()">
          {{ loading ? 'Checking...' : 'Try again' }}
        </button>
      </div>

      <ul v-if="brains.length" class="shared-brain-list" aria-label="Linked shared brains">
        <li v-for="brain in brains" :key="brain.name" class="shared-brain-row">
          <div class="shared-brain-head">
            <p class="shared-brain-name">{{ brain.name }}</p>
            <button
              type="button"
              class="set-link"
              :disabled="saving || listStale"
              :aria-label="`Remove ${brain.name}`"
              @click="removeBrain(brain)"
            >Remove</button>
          </div>
          <p class="shared-brain-identity">{{ brain.identity }}</p>
          <p class="shared-brain-path">{{ brain.path }}</p>
          <p
            class="shared-brain-status"
            :class="`shared-brain-status--${statusOf(brain).tone}`"
          >
            {{ statusOf(brain).text }}
          </p>
          <p v-if="statusOf(brain).detail" class="hint hint--compact">{{ statusOf(brain).detail }}</p>
          <p class="hint hint--compact">
            <span v-if="brain.branch">Branch {{ brain.branch }}</span>
            <span v-if="brain.branch"> · </span>
            <span v-if="brain.head">Checked-out commit {{ brain.head }}</span>
            <span v-else>Checked-out commit unknown</span>
            <span v-if="brain.dirty"> · Local changes</span>
          </p>
        </li>
      </ul>
      <p v-else class="hint">No shared brains are linked to this workspace yet.</p>

      <p v-if="saveError" class="action-result action-result--error" role="alert">{{ saveError }}</p>

      <form v-if="brains.length < MAX_SHARED_BRAINS" class="shared-brain-form" @submit.prevent="linkFolder">
        <label class="shared-brain-field">
          <span class="shared-brain-label">Folder path</span>
          <input
            v-model="pathDraft"
            type="text"
            class="routine-input shared-brain-input"
            autocomplete="off"
            spellcheck="false"
            placeholder="/absolute/path/to/team-notes"
            :disabled="saving || listStale"
          />
        </label>
        <label class="shared-brain-field">
          <span class="shared-brain-label">Name (optional)</span>
          <input
            v-model="nameDraft"
            type="text"
            class="routine-input shared-brain-input"
            autocomplete="off"
            spellcheck="false"
            placeholder="taken from the repository"
            :disabled="saving || listStale"
          />
        </label>
        <p class="hint hint--compact">The name cannot be changed later. Remove the brain and add it again to rename it.</p>
        <div class="action-row settings-actions">
          <button
            type="submit"
            class="btn-small"
            :disabled="saving || listStale || !pathDraft.trim()"
            :aria-busy="saving ? 'true' : undefined"
          >{{ saving ? 'Linking...' : 'Link folder' }}</button>
        </div>
      </form>
      <p v-else class="hint hint--compact" role="status">
        You can connect up to {{ MAX_SHARED_BRAINS }} shared brains. Remove one to add another.
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { askConfirm } from '../../lib/confirm'
import { apiErrorMessage } from '../../lib/errorMessage'
import { MAX_SHARED_BRAINS, SHARED_BRAIN_PRIVACY, sharedBrainStatus } from '../../lib/sharedBrain'
import { useProjectStore } from '../../stores/projects'
import type { SharedBrainRow, SharedBrainState, SharedBrainsResponse } from '../../lib/types'

// The card owns its data: it reads the linked brains on mount and whenever the
// active workspace changes, and saves the whole list through the workspace
// PATCH. Nothing here polls or runs an update; the engine does that.

const projectStore = useProjectStore()
const workspace = computed(() => projectStore.activeWorkspace)

const brains = ref<SharedBrainState[] | null>(null)
const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
// True from a successful save until a re-read succeeds: the list on screen is then
// older than the workspace, so adding or removing from it would resend a stale list.
const listStale = ref(false)
const saveError = ref('')
const pathDraft = ref('')
const nameDraft = ref('')
let ticket = 0

function statusOf(brain: SharedBrainState) {
  return sharedBrainStatus(brain)
}

/** The rows a save sends: every linked brain, as { path, name }. */
function currentRows(): SharedBrainRow[] {
  return (brains.value ?? []).map((brain) => ({ path: brain.path, name: brain.name }))
}

async function load(): Promise<void> {
  const ws = workspace.value
  const mine = ++ticket
  loading.value = true
  try {
    const res = await api.get<SharedBrainsResponse>(`/api/workspaces/${encodeURIComponent(ws)}/shared-brains`)
    if (mine !== ticket) return
    brains.value = res?.shared_brains ?? []
    loadError.value = ''
    listStale.value = false
  } catch (e) {
    if (mine !== ticket) return
    loadError.value = apiErrorMessage(e, 'The request failed.')
  } finally {
    if (mine === ticket) loading.value = false
  }
}

async function save(rows: SharedBrainRow[]): Promise<boolean> {
  const ws = workspace.value
  saving.value = true
  saveError.value = ''
  let ok = false
  try {
    await projectStore.updateWorkspace(ws, { shared_brains: rows })
    ok = true
  } catch (e) {
    if (ws === workspace.value) saveError.value = apiErrorMessage(e, 'The shared brains could not be saved.')
  } finally {
    // Unconditional: a save that outlives its workspace must not leave the new one disabled.
    saving.value = false
  }
  if (ok && ws === workspace.value) listStale.value = true
  return ok && ws === workspace.value
}

async function linkFolder(): Promise<void> {
  const path = pathDraft.value.trim()
  if (!path || saving.value || listStale.value) return
  const name = nameDraft.value.trim()
  const row: SharedBrainRow = name ? { path, name } : { path }
  const ok = await save([...currentRows(), row])
  if (!ok) return
  pathDraft.value = ''
  nameDraft.value = ''
  await load()
}

async function removeBrain(brain: SharedBrainState): Promise<void> {
  if (saving.value || listStale.value) return
  const ok = await askConfirm(
    `Disconnect ${brain.name}? Only this brain is disconnected. The folder, the other shared brains and your private notes are left untouched.`,
    { title: 'Disconnect this shared brain?', confirmLabel: 'Remove', destructive: true },
  )
  if (!ok || saving.value || listStale.value) return
  const rows = currentRows().filter((row) => row.path !== brain.path)
  const ok2 = await save(rows)
  if (ok2) await load()
}

onMounted(load)
watch(workspace, () => {
  // Another workspace's list must never stand in for this one's.
  brains.value = null
  loadError.value = ''
  listStale.value = false
  saving.value = false
  saveError.value = ''
  pathDraft.value = ''
  nameDraft.value = ''
  void load()
})
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.shared-brain-list { list-style: none; margin: var(--space-2) 0 0; padding: 0; display: grid; gap: var(--space-3); }
.shared-brain-row {
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  min-width: 0;
}
.shared-brain-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-2); }
.shared-brain-name { margin: 0; color: var(--fg); font-size: var(--text-sm); font-weight: 600; overflow-wrap: anywhere; }
.shared-brain-identity,
.shared-brain-path {
  margin: var(--space-1) 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}
.shared-brain-path { font-family: var(--font-mono); }
.shared-brain-status { margin: var(--space-2) 0 0; font-size: var(--text-sm); line-height: 1.5; overflow-wrap: anywhere; }
.shared-brain-status--ok { color: var(--fg); }
.shared-brain-status--warn { color: var(--warning); }
.shared-brain-status--error { color: var(--error); }
.shared-brain-stale { margin: 0 0 var(--space-2); }
.shared-brain-form { display: grid; gap: var(--space-3); margin-top: var(--space-3); min-width: 0; }
.shared-brain-field { display: grid; gap: var(--space-1); min-width: 0; }
.shared-brain-label { color: var(--fg); font-size: var(--text-sm); }
.shared-brain-input { width: 100%; min-width: 0; font-family: var(--font-mono); }
@media (pointer: coarse) {
  .shared-brain-input { min-height: var(--touch); font-size: 16px; }
  .shared-brain-head .set-link,
  .shared-brain-form .btn-small { min-height: var(--touch); min-width: var(--touch); }
}
</style>
