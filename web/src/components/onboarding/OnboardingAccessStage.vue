<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { apiErrorMessage } from '../../lib/errorMessage'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import { normalizeOverrides, setOverride } from '../../lib/workspaceOverrides'
import { BYPASS_NOTE, permissionDocsFor } from '../../lib/permissionDocs'
import OnboardingStack from './OnboardingStack.vue'
import OnboardingChoiceGroup from './OnboardingChoiceGroup.vue'
import OnboardingChoice from './OnboardingChoice.vue'

interface WorkspaceRow {
  name: string
  default_provider?: string
  provider_overrides?: Record<string, Record<string, string>>
}

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState] }>()

const provider = ref('')
const overrides = ref<Record<string, Record<string, string>>>({})
const approval = ref('')
const savedApproval = ref('')
// The engine-wide default for this provider, shown as inheritance rather than
// as an explicit choice.
const engineApproval = ref('')
const pending = ref(false)
const error = ref('')

// Every choice is written when it is made, so nothing stays dirty: an unsaved
// choice is one whose save failed, and that blocks Continue through `error`.
const canContinue = computed(() => !pending.value && !error.value)
const permissionDocs = computed(() => permissionDocsFor(provider.value))

function report(): void {
  emit('state', { dirty: false, pending: pending.value, canContinue: canContinue.value, error: error.value || null })
}
watch([approval, pending, error], report)

onMounted(async () => {
  try {
    const [workspaces, routines] = await Promise.all([
      api.get<{ workspaces?: WorkspaceRow[] }>('/api/workspaces'),
      api.get<{ provider_default_modes?: Record<string, string> }>('/api/settings/routines').catch((): { provider_default_modes?: Record<string, string> } => ({})),
    ])
    const row = workspaces.workspaces?.find(item => item.name === props.context.workspace)
    provider.value = row?.default_provider || ''
    overrides.value = normalizeOverrides(row?.provider_overrides)
    const stored = overrides.value[provider.value]?.permission_mode || ''
    approval.value = savedApproval.value = stored
    engineApproval.value = routines.provider_default_modes?.[provider.value] || ''
  } catch (e) {
    error.value = `Could not load access settings: ${apiErrorMessage(e, 'The request failed.')}`
  }
  report()
})

// Snapshot what is sent: a failed save puts the controls back to what is stored.
async function save(next: { approval?: string }): Promise<void> {
  const body: Record<string, unknown> = {}
  if (next.approval !== undefined) body.provider_overrides = setOverride(overrides.value, provider.value, 'permission_mode', next.approval)
  pending.value = true
  error.value = ''
  report()
  try {
    await api.patch(`/api/workspaces/${encodeURIComponent(props.context.workspace)}`, body)
    if (next.approval !== undefined) {
      savedApproval.value = next.approval
      // An empty value removes the override, which is how "engine default" is stored.
      overrides.value = setOverride(overrides.value, provider.value, 'permission_mode', next.approval)
    }
  } catch (e) {
    approval.value = savedApproval.value
    error.value = `Settings could not be saved: ${apiErrorMessage(e, 'The request failed.')}`
  } finally {
    pending.value = false
    report()
  }
}

function chooseApproval(value: string): void {
  if (pending.value) return
  if (value === savedApproval.value) { error.value = ''; return }
  approval.value = value
  void save({ approval: value })
}
</script>

<template>
  <OnboardingStack as="section" gap="xl" class="access-stage" aria-labelledby="access-title">
    <h2 id="access-title">Choose how the agent asks for approval</h2>

    <OnboardingChoiceGroup label="Approval mode" name="approval" layout="segmented" :model-value="approval" :disabled="!provider" @update:model-value="chooseApproval">
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
    <p class="hint" data-testid="bypass-note">
      {{ BYPASS_NOTE }}
      <a v-if="permissionDocs" :href="permissionDocs.href" target="_blank" rel="noopener">{{ permissionDocs.label }}</a>
    </p>

    <p v-if="error" class="error" role="alert" aria-live="assertive">{{ error }}</p>
  </OnboardingStack>
</template>

<style scoped>
.hint { margin: 0; color: var(--fg2); line-height: 1.5; }
@media (pointer: coarse) {
  .hint a { display: inline-flex; align-items: center; min-height: var(--touch, 44px); }
}
.error { margin: 0; color: var(--error-text); }
</style>
