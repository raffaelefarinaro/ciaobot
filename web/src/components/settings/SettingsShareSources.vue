<template>
  <section class="sources-panel" aria-labelledby="sources-heading">
    <div class="sources-header">
      <div>
        <p id="sources-heading" class="section-title">Sources and consent</p>
        <p class="hint">
          Choose one linked brain and one project source. A standing grant authorizes future structured
          decisions from that project only. An individual snapshot authorizes the exact decision you preview. Automatic sharing is not active yet; these permissions prepare for a future release.
        </p>
      </div>
      <p v-if="data" class="sources-mode" :class="`sources-mode--${data.mode}`">
        {{ data.mode === 'automatic' ? 'Standing permission recorded' : 'Review' }}
      </p>
    </div>

    <div v-if="!data && !loadError" class="hint" role="status">Reading the sources&hellip;</div>

    <template v-else-if="!data">
      <p class="action-result action-result--error" role="alert">
        Could not read the sources: {{ loadError }}
      </p>
      <button type="button" class="btn-secondary btn-small" :disabled="loading" @click="load()">
        {{ loading ? 'Checking...' : 'Try again' }}
      </button>
    </template>

    <template v-else>
      <p v-if="loadError" class="hint hint--warn" role="alert">
        Could not refresh: {{ loadError }}. Showing the last known sources.
      </p>
      <p v-if="actionError" class="action-result action-result--error" role="alert">{{ actionError }}</p>
      <p v-if="revokedNotice" class="action-result" role="status">{{ revokedNotice }}</p>

      <div v-if="!data.brains.length" class="hint">
        Link a shared brain above before you can authorize anything to leave this workspace.
      </div>
      <div v-else-if="!data.projects.length" class="hint">
        Create a project in this workspace before you authorize its decisions.
      </div>

      <template v-else>
        <div class="sources-grid">
          <label class="sources-field">
            <span class="sources-label">Shared brain</span>
            <select v-model="brainName" class="routine-input" aria-label="Shared brain" :disabled="busy">
              <option v-for="brain in data.brains" :key="brain.name" :value="brain.name">
                {{ brain.name }} — {{ brain.identity }}
              </option>
            </select>
          </label>

          <label class="sources-field">
            <span class="sources-label">Project</span>
            <select v-model="projectId" class="routine-input" aria-label="Project" :disabled="busy">
              <option v-for="project in data.projects" :key="project.project_id" :value="project.project_id">
                {{ project.name }}
              </option>
            </select>
          </label>

          <label class="sources-field">
            <span class="sources-label">Actual source</span>
            <select v-model="sourceId" class="routine-input" aria-label="Actual source" :disabled="busy || !sources.length">
              <option v-for="source in sources" :key="source.source_id" :value="source.source_id">
                {{ source.label }} ({{ source.entry_count }})
              </option>
            </select>
          </label>
        </div>

        <p v-if="!sources.length" class="hint hint--compact">
          This project has no decision source yet. Record the first structured decision below; the engine
          creates its own stream for this project.
        </p>

        <fieldset class="sources-group" :disabled="busy">
          <legend>What may leave</legend>
          <label class="sources-radio">
            <input v-model="grantType" type="radio" value="standing" aria-label="Future decisions from this stream" :disabled="sourceKind !== 'stream'">
            <span><strong>Future decisions from this stream</strong> — a matching later entry can be extracted with no individual approval.</span>
          </label>
          <label class="sources-radio">
            <input v-model="grantType" type="radio" value="snapshot" aria-label="Only this decision" :disabled="sourceKind !== 'document'">
            <span><strong>Only this decision</strong> — freezes the exact fields you preview; later edits need a fresh preview and grant.</span>
          </label>
        </fieldset>

        <fieldset class="sources-group" :disabled="busy">
          <legend>Exact exported fields</legend>
          <label v-for="field in data.fields" :key="field" class="sources-check">
            <input v-model="fieldMask" type="checkbox" :value="field" :aria-label="field">
            <span>{{ field }}</span>
          </label>
        </fieldset>

        <fieldset class="sources-group" :disabled="busy">
          <legend>Producers allowed to resolve this evidence</legend>
          <label v-for="producer in data.producers" :key="producer" class="sources-check">
            <input v-model="producers" type="checkbox" :value="producer" :aria-label="producer === 'memory_extraction' ? 'Memory extraction' : 'Nightly curation'">
            <span>{{ producer === 'memory_extraction' ? 'Memory extraction' : 'Nightly curation' }}</span>
          </label>
        </fieldset>

        <label v-if="grantType === 'snapshot'" class="sources-field sources-field--narrow">
          <span class="sources-label">Decision in {{ documentName }}</span>
          <select v-model.number="entryIndex" class="routine-input" :aria-label="`Decision in ${documentName}`" :disabled="busy">
            <option v-for="n in snapshotChoices" :key="n" :value="n - 1">Decision {{ n }}</option>
          </select>
        </label>

        <div class="action-row settings-actions">
          <button
            type="button"
            class="btn-small"
            :disabled="busy || !canPreview"
            :aria-busy="busy ? 'true' : undefined"
            @click="previewGrant"
          >{{ busy ? 'Working...' : 'Preview exact scope' }}</button>
        </div>

        <section v-if="previewData" class="sources-preview" aria-labelledby="preview-heading">
          <p id="preview-heading" class="sources-preview-title">Confirm this exact disclosure</p>
          <p class="sources-preview-summary">{{ previewData.preview.summary }}</p>
          <p v-if="previewData.preview.future_items_without_review" class="hint hint--warn">
            When automatic sharing becomes available, future items may be shared without another review. Entries recorded before this grant are not included.
          </p>
          <dl class="sources-scope">
            <dt>Scope</dt><dd>{{ previewData.preview.scope === 'future_decisions' ? 'Future decisions' : 'One decision' }}</dd>
            <dt>Source</dt><dd>{{ previewData.preview.source_label }}</dd>
            <dt>Entry</dt><dd>{{ previewData.preview.entry_label }}</dd>
            <dt>Recipient</dt><dd>{{ previewData.preview.recipient.brain }} — {{ previewData.preview.recipient.audience }}</dd>
            <dt>Producers</dt><dd>{{ previewData.preview.producers.join(', ') }}</dd>
            <dt>Excluded fields</dt><dd>{{ previewData.preview.excluded_fields.join(', ') || 'None' }}</dd>
          </dl>
          <div v-for="(sample, index) in previewData.preview.fields" :key="index" class="sources-sample">
            <p class="sources-sample-title">
              {{ previewData.preview.grant_type === 'snapshot' ? 'Exact exported values' : `Sample ${index + 1}` }}
            </p>
            <dl class="sources-scope">
              <template v-for="row in sample" :key="row.field">
                <dt>{{ row.field }}</dt><dd>{{ row.value }}</dd>
              </template>
            </dl>
          </div>
          <p class="hint hint--compact">{{ previewData.preview.notice }}</p>
          <div class="action-row settings-actions">
            <button type="button" class="btn-small" :disabled="busy" @click="confirmGrant">Confirm and grant</button>
            <button type="button" class="btn-secondary btn-small" :disabled="busy" @click="cancelPreview">Cancel</button>
          </div>
        </section>

        <section class="sources-decisions" aria-labelledby="decision-heading">
          <p id="decision-heading" class="sources-subtitle">Record a structured decision</p>
          <div v-if="activeDisclosures.length" class="hint hint--compact" role="note">
            <p>Standing disclosures active for this project stream:</p>
            <ul class="sources-disclosures">
              <li v-for="grant in activeDisclosures" :key="grant.grant_id">
                To {{ grant.brain.name }}: {{ grant.field_mask.join(', ') }} are permitted for future automatic sharing by
                {{ grant.producers.join(', ') }}.
              </li>
            </ul>
          </div>
          <p v-else class="hint hint--compact" role="note">
            No standing permission is recorded for this project stream.
          </p>
          <form class="sources-form" @submit.prevent="recordDecision">
            <label class="sources-field">
              <span class="sources-label">Decision</span>
              <input v-model="decisionDraft" type="text" class="routine-input" :disabled="busy" autocomplete="off">
            </label>
            <label class="sources-field">
              <span class="sources-label">Reason (optional)</span>
              <input v-model="reasonDraft" type="text" class="routine-input" :disabled="busy" autocomplete="off">
            </label>
            <label class="sources-field sources-field--narrow">
              <span class="sources-label">Effective date</span>
              <input v-model="dateDraft" type="date" class="routine-input" :disabled="busy">
            </label>
            <div class="action-row settings-actions">
              <button type="submit" class="btn-small" :disabled="busy || !decisionDraft.trim() || !dateDraft">
                {{ busy ? 'Saving...' : 'Save decision' }}
              </button>
            </div>
          </form>
        </section>

        <section class="sources-grants" aria-labelledby="grants-heading">
          <p id="grants-heading" class="sources-subtitle">Grants in this workspace</p>
          <p v-if="!data.grants.length" class="hint">No evidence grants yet.</p>
          <ul v-else class="sources-grant-list">
            <li v-for="grant in data.grants" :key="grant.grant_id" class="sources-grant-row">
              <div class="sources-grant-head">
                <p class="sources-grant-title">
                  {{ grant.type === 'standing' ? 'Future decisions' : 'One decision' }} ·
                  {{ grant.project_id }}
                </p>
                <span
                  class="sources-grant-state"
                  :class="{ 'sources-grant-state--revoked': grant.status !== 'active' }"
                >
                  {{ grantStatusText(grant) }}
                </span>
              </div>
              <p class="hint hint--compact">
                To {{ grant.brain.name }} ({{ grant.audience }}) · {{ grant.field_mask.join(', ') }} ·
                {{ grant.source_label }}
              </p>
              <p class="hint hint--compact sources-grant-id">{{ grant.grant_id }}</p>
              <p v-if="!grant.active" class="hint hint--compact">
                Generation {{ grant.revoked_generation ?? grant.consent_generation }} ended.
                A fresh preview and grant are required.
              </p>
              <div class="action-row settings-actions">
                <button type="button" class="btn-secondary btn-small" :disabled="busy" @click="toggleDetail(grant.grant_id)">
                  {{ openGrant === grant.grant_id ? 'Hide exact scope' : 'Inspect exact scope' }}
                </button>
                <button type="button" class="btn-secondary btn-small" :disabled="busy || !grant.active" @click="revokeGrant(grant)">
                  Revoke
                </button>
              </div>
              <div v-if="openGrant === grant.grant_id" class="sources-grant-detail">
                <dl class="sources-scope">
                  <dt>Grant</dt><dd>{{ grant.grant_id }}</dd>
                  <dt>Issued</dt><dd>{{ grant.issued_at }}</dd>
                  <dt>Generation</dt><dd>{{ grant.consent_generation }}</dd>
                  <dt>Brain link</dt><dd>{{ grant.brain.link_fingerprint || 'Unknown' }}</dd>
                  <dt>Producers</dt><dd>{{ grant.producers.join(', ') }}</dd>
                  <dt>Start</dt><dd>{{ grant.start_revision === null ? 'Exact previewed snapshot' : `Revision ${grant.start_revision}` }}</dd>
                  <dt>History</dt>
                  <dd>
                    <span v-for="(row, index) in grant.history" :key="index">
                      {{ row.event }} {{ row.event === 'issued' ? `gen ${row.generation}` : `gen ${row.generation}` }}<span v-if="index < grant.history.length - 1"> · </span>
                    </span>
                  </dd>
                </dl>
              </div>
            </li>
          </ul>
        </section>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { askConfirm } from '../../lib/confirm'
import { apiErrorMessage } from '../../lib/errorMessage'
import { useProjectStore } from '../../stores/projects'
import type {
  ShareEvidenceGrant,
  ShareEvidencePreview,
  ShareEvidenceState,
  ShareEvidenceSource,
} from '../../lib/types'

const projectStore = useProjectStore()
const workspace = computed(() => projectStore.activeWorkspace)

const data = ref<ShareEvidenceState | null>(null)
const loading = ref(false)
const loadError = ref('')
const actionError = ref('')
const revokedNotice = ref('')
const busy = ref(false)
const brainName = ref('')
const projectId = ref('')
const sourceId = ref('')
const grantType = ref<'standing' | 'snapshot'>('standing')
const fieldMask = ref<string[]>([])
const producers = ref<string[]>([])
const entryIndex = ref(0)
const decisionDraft = ref('')
const reasonDraft = ref('')
const dateDraft = ref('')
const previewData = ref<ShareEvidencePreview | null>(null)
const openGrant = ref('')
let ticket = 0

const projects = computed(() => data.value?.projects ?? [])
const selectedProject = computed(() => projects.value.find((p) => p.project_id === projectId.value))
const sources = computed<ShareEvidenceSource[]>(() => selectedProject.value?.sources ?? [])
const selectedSource = computed(() => sources.value.find((s) => s.source_id === sourceId.value))
const sourceKind = computed(() => selectedSource.value?.kind ?? '')
const snapshotChoices = computed(() => Array.from(
  { length: selectedSource.value?.entry_count ?? 0 },
  (_value, index) => index + 1,
))
const documentName = computed(() => selectedSource.value?.source_id.replace(/^document:/, '') ?? 'the document')
const activeDisclosures = computed(() => (data.value?.grants ?? []).filter((grant) =>
  grant.status === 'active'
  && grant.type === 'standing'
  && grant.project_id === projectId.value,
))
function grantStatusText(grant: ShareEvidenceGrant): string {
  if (grant.status === 'active') return 'Active'
  if (grant.status === 'unlinked') return 'Brain unlinked'
  if (grant.status === 'withdrawn') return 'Withdrawn'
  if (grant.status === 'revoked') return 'Revoked'
  return 'Inactive'
}
const canPreview = computed(() =>
  Boolean(brainName.value && projectId.value && sourceId.value && fieldMask.value.length && producers.value.length),
)

function resetSelection(): void {
  brainName.value = data.value?.brains[0]?.name ?? ''
  projectId.value = data.value?.projects[0]?.project_id ?? ''
  entryIndex.value = 0
  chooseSource()
}

function chooseSource(): void {
  const project = projects.value.find((p) => p.project_id === projectId.value)
  const available = project?.sources ?? []
  const preferred = available.find((source) => source.kind === 'stream') ?? available[0]
  sourceId.value = preferred?.source_id ?? ''
  grantType.value = preferred?.kind === 'document' ? 'snapshot' : 'standing'
}

watch(projectId, () => {
  if (!data.value) return
  chooseSource()
  cancelPreview()
})
watch(sourceId, () => {
  if (!selectedSource.value) return
  grantType.value = selectedSource.value.kind === 'document' ? 'snapshot' : 'standing'
  entryIndex.value = 0
  cancelPreview()
})
watch(grantType, () => cancelPreview())

async function load(): Promise<void> {
  const ws = workspace.value
  const mine = ++ticket
  loading.value = true
  try {
    const res = await api.get<ShareEvidenceState>(
      `/api/workspaces/${encodeURIComponent(ws)}/share-evidence`,
    )
    if (mine !== ticket) return
    data.value = res
    loadError.value = ''
    if (!res.brains.some((brain) => brain.name === brainName.value)) {
      resetSelection()
    } else if (!res.projects.some((project) => project.project_id === projectId.value)) {
      resetSelection()
    } else if (!sources.value.some((source) => source.source_id === sourceId.value)) {
      chooseSource()
    }
    if (!fieldMask.value.length) fieldMask.value = [...res.fields]
    if (!producers.value.length) producers.value = [...res.producers]
  } catch (e) {
    if (mine !== ticket) return
    loadError.value = apiErrorMessage(e, 'The request failed.')
  } finally {
    if (mine === ticket) loading.value = false
  }
}

function cancelPreview(): void {
  previewData.value = null
  actionError.value = ''
}

async function previewGrant(): Promise<void> {
  if (!canPreview.value || busy.value) return
  const ws = workspace.value
  busy.value = true
  actionError.value = ''
  revokedNotice.value = ''
  try {
    const res = await api.post<ShareEvidencePreview>(
      `/api/workspaces/${encodeURIComponent(ws)}/share-evidence/preview`,
      {
        brain: brainName.value,
        project_id: projectId.value,
        source_id: sourceId.value,
        grant_type: grantType.value,
        field_mask: fieldMask.value,
        producers: producers.value,
        ...(grantType.value === 'snapshot' ? { entry_index: entryIndex.value } : {}),
      },
    )
    if (ws === workspace.value) previewData.value = res
  } catch (e) {
    if (ws === workspace.value) actionError.value = apiErrorMessage(e, 'The scope could not be previewed.')
  } finally {
    busy.value = false
  }
}

async function confirmGrant(): Promise<void> {
  if (!previewData.value || busy.value) return
  const ws = workspace.value
  busy.value = true
  actionError.value = ''
  try {
    await api.post(`/api/workspaces/${encodeURIComponent(ws)}/share-evidence/confirm`, {
      preview_token: previewData.value.preview_token,
      confirm: true,
    })
    if (ws === workspace.value) {
      previewData.value = null
      revokedNotice.value = 'The grant was recorded for exactly the scope shown.'
      await load()
    }
  } catch (e) {
    if (ws === workspace.value) actionError.value = apiErrorMessage(e, 'The grant could not be confirmed.')
  } finally {
    busy.value = false
  }
}

async function recordDecision(): Promise<void> {
  if (busy.value || !decisionDraft.value.trim() || !dateDraft.value) return
  const ws = workspace.value
  busy.value = true
  actionError.value = ''
  try {
    await api.post(`/api/workspaces/${encodeURIComponent(ws)}/share-evidence/decisions`, {
      project_id: projectId.value,
      decision: decisionDraft.value.trim(),
      reason: reasonDraft.value.trim(),
      effective_date: dateDraft.value,
      disclosure_token: data.value?.disclosure_tokens[projectId.value],
    })
    if (ws === workspace.value) {
      decisionDraft.value = ''
      reasonDraft.value = ''
      dateDraft.value = ''
      await load()
    }
  } catch (e) {
    if (ws === workspace.value) {
      actionError.value = apiErrorMessage(e, 'The decision could not be saved.')
      if ((e as { status?: number }).status === 409) await load()
    }
  } finally {
    busy.value = false
  }
}

async function revokeGrant(grant: ShareEvidenceGrant): Promise<void> {
  if (busy.value || !grant.active) return
  const ok = await askConfirm(
    `Revoke this ${grant.type === 'standing' ? 'standing grant' : 'snapshot'} to ${grant.brain.name}? `
    + 'Earlier authorized evidence can no longer be extracted, and a fresh preview and grant are required.',
    { title: 'Revoke this disclosure?', confirmLabel: 'Revoke', destructive: true },
  )
  if (!ok || busy.value) return
  const ws = workspace.value
  busy.value = true
  actionError.value = ''
  try {
    await api.post(
      `/api/workspaces/${encodeURIComponent(ws)}/share-evidence/grants/${encodeURIComponent(grant.grant_id)}/revoke`,
      {},
    )
    if (ws === workspace.value) {
      revokedNotice.value = `Generation ${grant.consent_generation} was revoked. A fresh preview and grant are required.`
      await load()
    }
  } catch (e) {
    if (ws === workspace.value) actionError.value = apiErrorMessage(e, 'The grant could not be revoked.')
  } finally {
    busy.value = false
  }
}

function toggleDetail(grantId: string): void {
  openGrant.value = openGrant.value === grantId ? '' : grantId
}

onMounted(load)
watch(workspace, () => {
  data.value = null
  loadError.value = ''
  actionError.value = ''
  revokedNotice.value = ''
  previewData.value = null
  openGrant.value = ''
  brainName.value = ''
  projectId.value = ''
  sourceId.value = ''
  fieldMask.value = []
  producers.value = []
  void load()
})
</script>

<style scoped>
.sources-panel { display: grid; gap: var(--space-3); margin-top: var(--space-4); padding-top: var(--space-4); border-top: 1px solid var(--border); min-width: 0; }
.sources-header { display: flex; flex-wrap: wrap; align-items: start; justify-content: space-between; gap: var(--space-2); }
.sources-header .section-title { margin: 0; }
.sources-mode { margin: 0; font-size: var(--text-sm); color: var(--fg2); border: 1px solid var(--border); border-radius: 999px; padding: 2px 10px; }
.sources-mode--automatic { color: var(--fg); border-color: var(--accent); }
.sources-grid { display: grid; gap: var(--space-3); grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); min-width: 0; }
.sources-field { display: grid; gap: var(--space-1); min-width: 0; }
.sources-field--narrow { max-width: 240px; }
.sources-label { color: var(--fg); font-size: var(--text-sm); }
.sources-group { display: grid; gap: var(--space-2); margin: 0; padding: var(--space-2) var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-sm); min-width: 0; }
.sources-group legend { padding: 0 var(--space-1); color: var(--fg); font-size: var(--text-sm); }
.sources-check, .sources-radio { display: flex; gap: var(--space-2); align-items: start; font-size: var(--text-sm); color: var(--fg2); }
.sources-radio span { line-height: 1.5; }
.sources-preview, .sources-decisions, .sources-grants { display: grid; gap: var(--space-2); padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-sm); min-width: 0; }
.sources-preview-title, .sources-subtitle { margin: 0; color: var(--fg); font-size: var(--text-sm); font-weight: 600; }
.sources-preview-summary { margin: 0; color: var(--fg); font-size: var(--text-sm); }
.sources-scope { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--space-1) var(--space-3); margin: 0; font-size: var(--text-sm); }
.sources-scope dt { color: var(--fg2); }
.sources-scope dd { margin: 0; color: var(--fg); overflow-wrap: anywhere; }
.sources-sample { display: grid; gap: var(--space-1); padding: var(--space-2); background: var(--surface-2, transparent); border-radius: var(--radius-sm); }
.sources-sample-title { margin: 0; font-size: var(--text-sm); color: var(--fg2); }
.sources-form { display: grid; gap: var(--space-2); min-width: 0; }
.sources-disclosures { margin: var(--space-1) 0 0; padding-left: var(--space-4); display: grid; gap: var(--space-1); }
.sources-grant-list { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-2); }
.sources-grant-row { display: grid; gap: var(--space-1); padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-sm); min-width: 0; }
.sources-grant-head { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; justify-content: space-between; }
.sources-grant-title { margin: 0; color: var(--fg); font-size: var(--text-sm); font-weight: 600; overflow-wrap: anywhere; }
.sources-grant-state { font-size: var(--text-sm); color: var(--fg2); }
.sources-grant-state--revoked { color: var(--warning); }
.sources-grant-detail { padding-top: var(--space-1); }
@media (pointer: coarse) {
  .sources-panel select,
  .sources-panel input:not([type='checkbox']):not([type='radio']),
  .sources-panel .btn-small { min-height: var(--touch); }
  .sources-panel input[type='checkbox'],
  .sources-panel input[type='radio'] { width: var(--touch); height: var(--touch); }
}
</style>
