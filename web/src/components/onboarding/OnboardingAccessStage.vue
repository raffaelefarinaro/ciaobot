<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { apiErrorMessage } from '../../lib/errorMessage'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import { normalizeOverrides, setOverride } from '../../lib/workspaceOverrides'
import OnboardingStack from './OnboardingStack.vue'
import OnboardingButton from './OnboardingButton.vue'
import OnboardingChoiceGroup from './OnboardingChoiceGroup.vue'
import OnboardingChoice from './OnboardingChoice.vue'

interface WorkspaceRow {
  name: string
  agent_fs_scope?: string | null
  default_provider?: string
  provider_overrides?: Record<string, Record<string, string>>
}

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState] }>()

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

// Every choice is written when it is made, so nothing stays dirty: an unsaved
// choice is one whose save failed, and that blocks Continue through `error`.
// The scope before the last change, so a risky choice can be undone.
const previousScope = ref('')
const canContinue = computed(() => !!scope.value && !pending.value && !error.value)
// Exact match: "darwin" contains "win", which would show the Windows warning
// to every macOS user and lock them out of workspace scope.
const windows = computed(() => platform.value === 'windows' || platform.value === 'win32')

function report(): void {
  emit('state', { dirty: false, pending: pending.value, canContinue: canContinue.value, error: error.value || null })
}
watch([scope, approval, pending, error], report)

onMounted(async () => {
  try {
    const [workspaces, host, routines] = await Promise.all([
      api.get<{ workspaces?: WorkspaceRow[] }>('/api/workspaces'),
      api.get<{ platform?: string; available?: boolean }>('/api/host/terminal').catch((): { platform?: string } => ({})),
      api.get<{ provider_default_modes?: Record<string, string> }>('/api/settings/routines').catch((): { provider_default_modes?: Record<string, string> } => ({})),
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
    // The host route reports "other" for an unsupported system; that is not a
    // platform whose sandbox behaviour we can describe.
    platformKnown.value = ['macos', 'darwin', 'windows', 'win32', 'linux'].includes(platform.value)
  } catch (e) {
    error.value = `Could not load access settings: ${apiErrorMessage(e, 'The request failed.')}`
  }
  report()
})

// Snapshot what is sent: a failed save puts the controls back to what is stored.
async function save(next: { scope?: string; approval?: string }): Promise<void> {
  const body: Record<string, unknown> = {}
  if (next.scope !== undefined) body.agent_fs_scope = next.scope
  if (next.approval !== undefined) body.provider_overrides = setOverride(overrides.value, provider.value, 'permission_mode', next.approval)
  pending.value = true
  error.value = ''
  report()
  try {
    await api.patch(`/api/workspaces/${encodeURIComponent(props.context.workspace)}`, body)
    if (next.scope !== undefined) { previousScope.value = savedScope.value; savedScope.value = next.scope }
    if (next.approval !== undefined) {
      savedApproval.value = next.approval
      // An empty value removes the override, which is how "engine default" is stored.
      overrides.value = setOverride(overrides.value, provider.value, 'permission_mode', next.approval)
    }
  } catch (e) {
    scope.value = savedScope.value
    approval.value = savedApproval.value
    error.value = `Settings could not be saved: ${apiErrorMessage(e, 'The request failed.')}`
  } finally {
    pending.value = false
    report()
  }
}

function chooseScope(value: string): void {
  if (value === savedScope.value) return
  scope.value = value
  void save({ scope: value })
}
function chooseApproval(value: string): void {
  if (value === savedApproval.value) return
  approval.value = value
  void save({ approval: value })
}
function undoScope(): void { chooseScope(previousScope.value) }
</script>

<template>
  <OnboardingStack as="section" gap="xl" class="access-stage" aria-labelledby="access-title">
    <h2 id="access-title">Choose what the agent can access</h2>

    <OnboardingChoiceGroup label="Filesystem scope" name="scope" layout="cards" :model-value="scope" :disabled="pending" @update:model-value="chooseScope">
      <OnboardingChoice value="workspace" :disabled="windows">
        Workspace only
        <template #description>Access stays inside this workspace.</template>
      </OnboardingChoice>
      <OnboardingChoice value="machine">
        Whole machine
        <template #description>Access may include any file this user can read.</template>
      </OnboardingChoice>
      <template #after>
        <p v-if="scope === 'machine'" class="consequence" role="status">
          The agent and its shell can read any file this user can read, outside this workspace too.
          <OnboardingButton v-if="previousScope === 'workspace' && !windows" variant="link" class="undo" :disabled="pending" @click="undoScope">Undo</OnboardingButton>
        </p>
        <p v-if="windows" class="hint">Windows has no filesystem sandbox; workspace-only scope is unavailable.</p>
        <p v-else-if="platformKnown" class="hint">Scope limits file access; it is not a safety guarantee.</p>
        <p v-else class="hint">This host's platform was not reported, so no scope can be recommended. Choose one explicitly.</p>
      </template>
    </OnboardingChoiceGroup>

    <OnboardingChoiceGroup label="Approval mode" name="approval" layout="segmented" :model-value="approval" :disabled="pending || !provider" @update:model-value="chooseApproval">
      <OnboardingChoice value="">Engine default{{ engineApproval ? ` (${engineApproval})` : ' (unknown)' }}</OnboardingChoice>
      <OnboardingChoice value="auto">Auto</OnboardingChoice>
      <OnboardingChoice value="manual">Manual approvals</OnboardingChoice>
      <OnboardingChoice value="bypass">Bypass permissions</OnboardingChoice>
      <template #after>
        <p v-if="!provider" class="hint">The runtime is not known yet, so approval mode is unknown.</p>
        <p v-else-if="provider === 'claude'" class="hint">Recommended: Claude Auto. A classifier decides what is routine and what needs an approval card. This is not a safety guarantee.</p>
        <p v-else-if="provider === 'opencode'" class="hint">OpenCode manual approvals use configurable rules. Bypass relies on instructions to the model; those instructions are not enforced safeguards.</p>
        <p v-else class="hint">No approval guidance is available for this runtime.</p>
      </template>
    </OnboardingChoiceGroup>

    <p v-if="error" class="error" role="alert" aria-live="assertive">{{ error }}</p>
  </OnboardingStack>
</template>

<style scoped>
.hint { margin: 0; color: var(--fg2); line-height: 1.5; }
.consequence { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3, 12px); margin: 0; line-height: 1.5; }
.error { margin: 0; color: var(--error); }
</style>
