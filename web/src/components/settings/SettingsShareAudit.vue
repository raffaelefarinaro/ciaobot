<template>
  <section class="audit-panel" aria-labelledby="audit-heading">
    <div>
      <p id="audit-heading" class="section-title">Consent and history</p>
      <p class="hint">
        This view only shows what Ciaobot has recorded. It changes nothing. Every brain starts in review, where you
        approve each note yourself. Automatic sharing is not active yet.
      </p>
    </div>

    <div v-if="!data && !loadError" class="hint" role="status">Reading the consent record&hellip;</div>

    <template v-else-if="!data">
      <p class="action-result action-result--error" role="alert">Could not read the consent record: {{ loadError }}</p>
      <button type="button" class="btn-secondary btn-small" :disabled="loading" @click="load()">
        {{ loading ? 'Checking...' : 'Try again' }}
      </button>
    </template>

    <template v-else>
      <p v-if="loadError" class="hint hint--warn" role="alert">
        Could not refresh: {{ loadError }}. Showing the last known record.
      </p>

      <ul class="audit-list" aria-label="Consent by shared brain">
        <li v-for="group in groups" :key="group.key" class="audit-brain">
          <p class="audit-title">{{ group.name }}</p>
          <p v-if="group.identity" class="hint hint--compact audit-wrap">{{ group.identity }}</p>
          <p class="audit-policy">{{ group.policy }}</p>
          <p v-for="line in audienceLines(group.target)" :key="line" class="hint hint--compact audit-wrap">{{ line }}</p>
          <p class="hint hint--compact">{{ retentionText(group.kind) }}</p>

          <p v-if="!group.grants.length" class="hint hint--compact">No project permission is recorded for this brain.</p>
          <ul v-else class="audit-grants" :aria-label="`Permissions for ${group.name}`">
            <li v-for="grant in group.grants" :key="grant.grant_id" class="audit-grant">
              <div class="audit-grant-head">
                <p class="audit-grant-title">{{ grantLabel(grant) }}</p>
                <span class="audit-state" :class="{ 'audit-state--ended': grant.status !== 'active' }">{{ grantStatusLabel(grant) }}</span>
              </div>
              <dl class="audit-scope">
                <dt>Project</dt><dd>{{ grant.project_id }}</dd>
                <dt>Source</dt><dd>{{ grant.source_label || 'Unknown' }}</dd>
                <dt>Fields</dt><dd>{{ grant.field_mask.join(', ') || 'None' }}</dd>
                <dt>Producers</dt><dd>{{ grant.producers.map(producerLabel).join(', ') || 'None' }}</dd>
                <dt>Audience</dt><dd>Unknown</dd>
                <dt>Granted</dt><dd>{{ localTime(grant.issued_at) }}, generation {{ grant.consent_generation }}</dd>
                <template v-if="grant.revoked_at"><dt>Revoked</dt><dd>{{ localTime(grant.revoked_at) }}</dd></template>
                <template v-if="grant.withdrawn_at"><dt>Withdrawn</dt><dd>{{ localTime(grant.withdrawn_at) }}</dd></template>
              </dl>
              <p v-if="grant.type === 'standing'" class="hint hint--compact">
                A standing permission covers later matching entries. Entries recorded before it are not included.
              </p>
            </li>
          </ul>
        </li>
      </ul>
      <p v-if="!groups.length" class="hint">No shared brains are linked to this workspace yet.</p>

      <section aria-labelledby="audit-history-heading" class="audit-history">
        <p id="audit-history-heading" class="audit-title">History</p>
        <p v-if="!events.length" class="hint hint--compact">No grants or revocations have been recorded.</p>
        <ol v-else class="audit-events" aria-label="Grant and revocation history, newest first">
          <li v-for="event in events" :key="event.key" class="audit-event">
            <span class="audit-event-text">{{ event.text }}</span>
            <span class="hint hint--compact audit-wrap">{{ event.grantLabel }} · {{ event.when }}</span>
          </li>
        </ol>
        <p class="hint hint--compact">
          A revocation stops future sharing. It does not remove a note that was already published.
        </p>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { apiErrorMessage } from '../../lib/errorMessage'
import { sharedBrainPolicyText } from '../../lib/sharedBrain'
import {
  audienceLines,
  auditEvents,
  grantLabel,
  grantStatusLabel,
  localTime,
  producerLabel,
  retentionText,
} from '../../lib/shareAudit'
import { useProjectStore } from '../../stores/projects'
import type { SharedBrainState, ShareEvidenceGrant, ShareEvidenceState } from '../../lib/types'

// Read-only: it reads the evidence record and shows it. The brains come from
// the card that mounts this view, so a policy change there shows here too.
const props = defineProps<{ brains: SharedBrainState[] }>()

const projectStore = useProjectStore()
const workspace = computed(() => projectStore.activeWorkspace)
const data = ref<ShareEvidenceState | null>(null)
const loading = ref(false)
const loadError = ref('')
let ticket = 0

interface Group {
  key: string
  name: string
  identity: string
  target: string
  kind: string
  policy: string
  grants: ShareEvidenceGrant[]
}

function policyFor(brain: SharedBrainState): string {
  return brain.kind === 'folder'
    ? 'A folder is never shared automatically. You review each note before it is saved.'
    : sharedBrainPolicyText(brain)
}

const groups = computed<Group[]>(() => {
  const grants = data.value?.grants ?? []
  const linked = new Set(props.brains.map((brain) => brain.name))
  const rows: Group[] = props.brains.map((brain) => ({
    key: brain.name,
    name: brain.name,
    identity: brain.identity,
    target: brain.identity,
    kind: brain.kind,
    policy: policyFor(brain),
    grants: grants.filter((grant) => grant.brain.name === brain.name),
  }))
  const orphans = grants.filter((grant) => !linked.has(grant.brain.name))
  for (const name of new Set(orphans.map((grant) => grant.brain.name))) {
    rows.push({
      key: `unlinked:${name}`,
      name: name || 'Unknown brain',
      identity: '',
      target: '',
      kind: 'git',
      policy: 'This brain is no longer linked. Its old permissions cannot be used.',
      grants: orphans.filter((grant) => grant.brain.name === name),
    })
  }
  return rows
})

const events = computed(() => auditEvents(data.value?.grants ?? []))

async function load(): Promise<void> {
  const ws = workspace.value
  const mine = ++ticket
  loading.value = true
  try {
    const res = await api.get<ShareEvidenceState>(`/api/workspaces/${encodeURIComponent(ws)}/share-evidence`)
    if (mine !== ticket) return
    data.value = res
    loadError.value = ''
  } catch (e) {
    if (mine !== ticket) return
    loadError.value = apiErrorMessage(e, 'The request failed.')
  } finally {
    if (mine === ticket) loading.value = false
  }
}

onMounted(load)
watch(workspace, () => {
  data.value = null
  loadError.value = ''
  void load()
})
</script>

<style scoped>
.audit-panel { display: grid; gap: var(--space-3); margin-top: var(--space-4); padding-top: var(--space-4); border-top: 1px solid var(--border); min-width: 0; }
.audit-panel .section-title { margin: 0; }
.audit-list, .audit-grants, .audit-events { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-2); min-width: 0; }
.audit-brain, .audit-history { display: grid; gap: var(--space-1); padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-sm); min-width: 0; }
.audit-grant { display: grid; gap: var(--space-1); padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-sm); min-width: 0; }
.audit-title, .audit-grant-title { margin: 0; color: var(--fg); font-size: var(--text-sm); font-weight: 600; overflow-wrap: anywhere; }
.audit-policy { margin: 0; color: var(--fg); font-size: var(--text-sm); }
.audit-grant-head { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; justify-content: space-between; }
.audit-state { font-size: var(--text-sm); color: var(--fg2); }
.audit-state--ended { color: var(--warning); }
.audit-scope { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--space-1) var(--space-3); margin: 0; font-size: var(--text-sm); }
.audit-scope dt { color: var(--fg2); }
.audit-scope dd { margin: 0; color: var(--fg); overflow-wrap: anywhere; }
.audit-event { display: grid; gap: 2px; font-size: var(--text-sm); color: var(--fg); }
.audit-wrap { overflow-wrap: anywhere; }
</style>
