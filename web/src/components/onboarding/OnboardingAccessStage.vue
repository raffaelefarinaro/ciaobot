<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { apiErrorMessage } from '../../lib/errorMessage'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import { normalizeOverrides, setOverride } from '../../lib/workspaceOverrides'

interface WorkspaceRow {
  name: string
  agent_fs_scope?: string | null
  default_provider?: string
  provider_overrides?: Record<string, Record<string, string>>
}

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState]; continue: []; back: []; refresh: [] }>()

const scope = ref('')
const savedScope = ref('')
const provider = ref('')
const overrides = ref<Record<string, Record<string, string>>>({})
const approval = ref('')
const savedApproval = ref('')
// The engine-wide default for this provider, shown as inheritance rather than
// as an explicit choice.
const engineApproval = ref('')
const platform = ref('')
const platformKnown = ref(false)
const pending = ref(false)
const error = ref('')

const dirty = computed(() => scope.value !== savedScope.value || approval.value !== savedApproval.value)
const canContinue = computed(() => !!scope.value && !dirty.value && !pending.value && !error.value)
const windows = computed(() => platformKnown.value && platform.value.includes('win'))

function report(): void {
  emit('state', { dirty: dirty.value, pending: pending.value, canContinue: canContinue.value, error: error.value || null })
}
watch([scope, approval, pending, error], report)

onMounted(async () => {
  try {
    const [workspaces, host, routines] = await Promise.all([
      api.get<{ workspaces?: WorkspaceRow[] }>('/api/workspaces'),
      api.get<{ platform?: string; available?: boolean }>('/api/host/terminal').catch(() => ({})),
      api.get<{ provider_default_modes?: Record<string, string> }>('/api/settings/routines').catch(() => ({})),
    ])
    const row = workspaces.workspaces?.find(item => item.name === props.context.workspace)
    // Only an explicit stored value is a scope; anything else is still unknown.
    scope.value = savedScope.value =
      row?.agent_fs_scope === 'workspace' || row?.agent_fs_scope === 'machine' ? row.agent_fs_scope : ''
    provider.value = row?.default_provider || ''
    overrides.value = normalizeOverrides(row?.provider_overrides)
    const stored = overrides.value[provider.value]?.permission_mode || ''
    approval.value = savedApproval.value = stored
    engineApproval.value = routines.provider_default_modes?.[provider.value] || ''
    platform.value = String(host.platform || '').toLowerCase()
    platformKnown.value = !!platform.value
  } catch (e) {
    error.value = `Could not load access settings: ${apiErrorMessage(e, 'The request failed.')}`
  }
  report()
})

async function save(): Promise<void> {
  // Snapshot what is being sent: edits during the request must stay unsaved.
  const submitted = { scope: scope.value, approval: approval.value }
  pending.value = true
  error.value = ''
  report()
  try {
    await api.patch(`/api/workspaces/${encodeURIComponent(props.context.workspace)}`, {
      ...(submitted.scope !== savedScope.value ? { agent_fs_scope: submitted.scope } : {}),
      ...(submitted.approval !== savedApproval.value
        ? { provider_overrides: setOverride(overrides.value, provider.value, 'permission_mode', submitted.approval) }
        : {}),
    })
    savedScope.value = submitted.scope
    savedApproval.value = submitted.approval
    // Mirror only the field we sent; an empty value removes the override, which
    // is how "engine default" is stored.
    overrides.value = setOverride(overrides.value, provider.value, 'permission_mode', submitted.approval)
  } catch (e) {
    error.value = `Settings could not be saved: ${apiErrorMessage(e, 'The request failed.')}`
  } finally {
    pending.value = false
    report()
  }
}
</script>

<template>
  <section class="access-stage" aria-labelledby="access-title">
    <h2 id="access-title">Choose what the agent can access</h2>

    <fieldset>
      <legend>Filesystem scope</legend>
      <p>Choose the files the agent and its shell may access.</p>
      <label>
        <input v-model="scope" type="radio" value="workspace" :disabled="pending || windows" />
        Workspace only <span>Access stays inside this workspace.</span>
      </label>
      <label>
        <input v-model="scope" type="radio" value="machine" :disabled="pending" />
        Whole machine <span>Access may include any file this user can read.</span>
      </label>
      <p v-if="windows" class="hint">Windows has no filesystem sandbox; workspace-only scope is unavailable.</p>
      <p v-else-if="platformKnown" class="hint">Scope limits file access; it is not a safety guarantee.</p>
      <p v-else class="hint">This host's platform was not reported, so no scope can be recommended. Choose one explicitly.</p>
    </fieldset>

    <fieldset>
      <legend>Approval mode</legend>
      <label>
        Mode
        <select v-model="approval" aria-label="Approval mode" :disabled="pending || !provider">
          <option value="">Engine default{{ engineApproval ? ` (${engineApproval})` : ' (unknown)' }}</option>
          <option value="auto">Auto</option>
          <option value="manual">Manual approvals</option>
          <option value="bypass">Bypass permissions</option>
        </select>
      </label>
      <p v-if="!provider" class="hint">The runtime is not known yet, so approval mode is unknown.</p>
      <p v-else-if="provider === 'claude'">Recommended: Claude Auto. A classifier decides what is routine and what needs an approval card. This is not a safety guarantee.</p>
      <p v-else-if="provider === 'opencode'">OpenCode manual approvals use configurable rules. Bypass relies on instructions to the model; those instructions are not enforced safeguards.</p>
      <p v-else class="hint">No approval guidance is available for this runtime.</p>
    </fieldset>

    <p v-if="error" role="alert" aria-live="assertive">{{ error }}</p>
    <button type="button" :disabled="pending || !dirty" @click="save">{{ pending ? 'Saving…' : 'Save choices' }}</button>
    <button type="button" :disabled="!canContinue || props.busy" @click="emit('continue')">Continue</button>
    <button type="button" @click="emit('back')">Back</button>
  </section>
</template>

<style scoped>
.access-stage { display: grid; gap: 12px; }
fieldset { border: 1px solid var(--border); border-radius: var(--radius-sm, 6px); padding: 8px 12px; }
legend { padding: 0 4px; color: var(--fg2); }
label { display: flex; align-items: center; gap: 8px; min-height: 44px; }
span, .hint { color: var(--fg2); }
button { min-height: 44px; padding: 8px 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: transparent; color: var(--fg); font: inherit; cursor: pointer; }
button:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button:disabled { opacity: .55; cursor: not-allowed; }
</style>
