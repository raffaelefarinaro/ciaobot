<template>
  <section class="project-section decision-entry" aria-labelledby="project-decisions-title">
    <div class="decision-heading">
      <h3 id="project-decisions-title">Decisions</h3>
      <button v-if="!open" type="button" class="text-action" @click="show">Record decision</button>
    </div>
    <p v-if="!open" class="decision-hint">Record a structured decision for this project.</p>
    <template v-else>
      <p v-if="loading" role="status">Loading sharing permissions…</p>
      <p v-else-if="loadError" role="alert">{{ loadError }} <button type="button" class="text-action" @click="load">Retry</button> <button type="button" class="text-action" @click="open = false">Close</button></p>
      <template v-else>
        <div role="note" class="decision-hint">
          <p>Automatic sharing is not active yet. The following permissions prepare for a future release.</p>
          <ul v-if="grants.length">
            <li v-for="grant in grants" :key="grant.grant_id">{{ grant.brain.name }} ({{ grant.audience }}): {{ grant.field_mask.join(', ') }} for {{ grant.producers.join(', ') }}.</li>
          </ul>
          <p v-else>No standing permission is recorded for this project.</p>
        </div>
        <form @submit.prevent="save">
          <label>Decision <input v-model="decision" type="text" maxlength="2000" required :disabled="saving"></label>
          <label>Reason (optional) <input v-model="reason" type="text" maxlength="2000" :disabled="saving"></label>
          <label>Effective date <input v-model="date" type="date" required :disabled="saving"></label>
          <p v-if="error" role="alert">{{ error }}</p>
          <p v-if="saved" role="status">Decision recorded.</p>
          <div class="decision-actions">
            <button type="submit" class="btn-small" :disabled="saving || !decision.trim() || !date">{{ saving ? 'Saving…' : 'Save decision' }}</button>
            <button type="button" class="btn-small" :disabled="saving" @click="open = false">Close</button>
          </div>
        </form>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../lib/api'
import { apiErrorMessage } from '../lib/errorMessage'
import type { ShareEvidenceGrant, ShareEvidenceState } from '../lib/types'

const props = defineProps<{ workspace: string; projectId: string }>()
const open = ref(false)
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const error = ref('')
const saved = ref(false)
const decision = ref('')
const reason = ref('')
const date = ref('')
const grants = ref<ShareEvidenceGrant[]>([])
const endpoint = `/api/workspaces/${encodeURIComponent(props.workspace)}/share-evidence`

async function show() {
  open.value = true
  await load()
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const state = await api.get<ShareEvidenceState>(endpoint)
    grants.value = state.grants.filter(grant => grant.project_id === props.projectId && grant.type === 'standing' && grant.status === 'active')
  } catch (e) {
    loadError.value = apiErrorMessage(e, 'Sharing permissions could not be loaded.')
  } finally {
    loading.value = false
  }
}

async function save() {
  if (saving.value || !decision.value.trim() || !date.value) return
  saving.value = true
  error.value = ''
  saved.value = false
  try {
    await api.post(`${endpoint}/decisions`, { project_id: props.projectId, decision: decision.value, reason: reason.value, effective_date: date.value })
    decision.value = ''
    reason.value = ''
    date.value = ''
    saved.value = true
  } catch (e) {
    error.value = apiErrorMessage(e, 'The decision could not be recorded.')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.decision-heading { display: flex; align-items: center; justify-content: space-between; }
.decision-hint { color: var(--fg2); font-size: var(--text-sm); }
form { display: grid; gap: 12px; }
label { display: grid; gap: 4px; }
input { width: 100%; box-sizing: border-box; font: inherit; }
.decision-actions { display: flex; gap: 8px; }
@media (pointer: coarse) { input, button { min-height: 44px; } input { font-size: 16px; } }
</style>
