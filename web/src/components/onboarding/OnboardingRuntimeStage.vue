<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import type { HostTerminalAction, ModelsResponse, RuntimeProvider, WorkspaceInfo } from '../../lib/types'
import { apiErrorMessage } from '../../lib/errorMessage'
import { normalizeOverrides, setOverride } from '../../lib/workspaceOverrides'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import HostTerminalButton from '../HostTerminalButton.vue'
import OnboardingStack from './OnboardingStack.vue'
import OnboardingButton from './OnboardingButton.vue'
import OnboardingChoiceGroup from './OnboardingChoiceGroup.vue'
import OnboardingChoice from './OnboardingChoice.vue'

interface ProviderStatus {
  ok?: boolean
  auth?: string
  command?: string
  terminal_action?: HostTerminalAction
  label?: string
}

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState] }>()

const PROVIDER_LABELS: Record<RuntimeProvider, string> = { claude: 'Claude Code', opencode: 'OpenCode' }

const connections = ref<Record<string, ProviderStatus> | null>(null)
const models = ref<ModelsResponse | null>(null)
const loadingStatus = ref(false)
const loadingModels = ref(false)
// Kept apart: a failed status read must not be erased by a later model read,
// which would let the shell advance on a status nobody confirmed.
const statusError = ref('')
const modelsError = ref('')
const saveError = ref('')
const saving = ref(false)
const error = computed(() => statusError.value || modelsError.value || saveError.value)
const provider = ref<RuntimeProvider>('claude')
const selection = ref('')
const deferred = ref(false)
// What the workspace already has (set by the installer or an earlier visit).
// It is shown as set until the person asks to change it.
const current = ref<{ provider: RuntimeProvider; model: string } | null>(null)
const editing = ref(true)
// The provider and model last written, so Continue does not write twice.
const savedKey = ref('')

const connection = computed<ProviderStatus | undefined>(() => connections.value?.[provider.value])
const evidence = computed(() => models.value?.provider_catalog_evidence?.[provider.value])
const reported = computed(() => (evidence.value?.status === 'observed' ? evidence.value.models : []))
const configured = computed(() => models.value?.provider_models?.[provider.value] ?? [])
// `auth` is the provider's own sentence ("signed in as …", "not signed in",
// "not installed"). Only a clearly signed-in provider may skip repair.
const signedIn = computed(() => !!connection.value?.ok && !/not|missing|no account|signed out|error/i.test(connection.value?.auth ?? ''))
// Readiness needs an actual row for the selected runtime; `{connections: {}}` is
// not evidence that this provider is usable.
const ready = computed(() => !!connection.value && !error.value)
const terminalAction = computed<HostTerminalAction | undefined>(() => connection.value?.terminal_action)
const providerLabel = computed(() => connection.value?.label ?? PROVIDER_LABELS[provider.value])
const state = computed<StageState>(() => ({
  dirty: false,
  pending: loadingStatus.value || loadingModels.value || saving.value,
  canContinue: !saving.value && ready.value && (!!selection.value || deferred.value),
  error: error.value || null,
}))

watch(state, value => emit('state', value), { immediate: true })

async function loadCurrent(): Promise<void> {
  try {
    const payload = await api.get<{ workspaces: WorkspaceInfo[] }>('/api/workspaces')
    const row = payload.workspaces.find(item => item.name === props.context.workspace)
    const chosen = row?.default_provider
    const model = chosen ? normalizeOverrides(row?.provider_overrides)[chosen]?.model : ''
    if ((chosen === 'claude' || chosen === 'opencode') && model) {
      current.value = { provider: chosen, model }
      provider.value = chosen
      selection.value = model
      savedKey.value = `${chosen}/${model}`
      editing.value = false
    }
  } catch {
    // Not knowing the current choice only means it is asked, never assumed.
  }
}

async function loadStatus(): Promise<boolean> {
  loadingStatus.value = true
  statusError.value = ''
  try {
    const payload = await api.get<{ connections: Record<string, ProviderStatus> }>('/api/settings/providers')
    connections.value = payload.connections ?? {}
    return true
  } catch {
    statusError.value = 'Could not read the provider status. Retry, or continue later with a deferred choice.'
    return false
  } finally {
    loadingStatus.value = false
  }
}

async function loadModels(): Promise<void> {
  loadingModels.value = true
  modelsError.value = ''
  try {
    models.value = await api.get<ModelsResponse>(`/api/models?workspace=${encodeURIComponent(props.context.workspace)}`)
  } catch {
    modelsError.value = 'Could not load model information. Retry, or continue later with a deferred choice.'
  } finally {
    loadingModels.value = false
  }
}

// The list appears without being asked: once a signed-in runtime is selected
// and the person is choosing, its models are read once and reused.
async function showModels(): Promise<void> {
  if (!editing.value || !signedIn.value || models.value !== null || loadingModels.value) return
  await loadModels()
}

// Retry the operation that actually failed: a model read is not retried by a
// status check, and vice versa.
async function retry(): Promise<void> {
  if (statusError.value) await checkAgain()
  else if (modelsError.value) await loadModels()
  else if (saveError.value) await save()
}

async function checkAgain(): Promise<void> {
  const ok = await loadStatus()
  if (!ok) return
  if (models.value !== null) await loadModels()
  else await showModels()
}

function selectProvider(id: string): void {
  if (saving.value || (id !== 'claude' && id !== 'opencode')) return
  provider.value = id
  saveError.value = ''
  selection.value = ''
  deferred.value = false
  void showModels()
}

// Choosing a model saves it; there is no separate save step.
async function choose(id: string): Promise<void> {
  if (saving.value) return
  saveError.value = ''
  selection.value = id
  deferred.value = false
  // A failed write keeps the selection so Retry can write the same choice.
  await save()
}

function defer(): void {
  saveError.value = ''
  selection.value = ''
  deferred.value = true
}

async function change(): Promise<void> {
  editing.value = true
  await showModels()
}

async function save(): Promise<boolean> {
  if (saving.value || loadingStatus.value || loadingModels.value || !connection.value || statusError.value || modelsError.value) return false
  if (deferred.value) return true
  if (!selection.value) return false
  const chosenProvider = provider.value
  const chosenModel = selection.value
  if (savedKey.value === `${chosenProvider}/${chosenModel}`) return true
  saving.value = true
  saveError.value = ''
  try {
    // Read at save time so choosing a model keeps the workspace's other
    // provider settings, including approval and thinking overrides.
    const payload = await api.get<{ workspaces: WorkspaceInfo[] }>('/api/workspaces')
    const workspace = payload.workspaces.find(item => item.name === props.context.workspace)
    if (!workspace) throw new Error('The workspace is no longer available.')
    await api.patch(`/api/workspaces/${encodeURIComponent(props.context.workspace)}`, {
      default_provider: chosenProvider,
      provider_overrides: setOverride(normalizeOverrides(workspace.provider_overrides), chosenProvider, 'model', chosenModel),
    })
    savedKey.value = `${chosenProvider}/${chosenModel}`
    current.value = { provider: chosenProvider, model: chosenModel }
    return true
  } catch (reason) {
    saveError.value = apiErrorMessage(reason, 'The runtime and model could not be saved.')
    return false
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await loadCurrent()
  if (await loadStatus()) await showModels()
})

defineExpose({ refresh: checkAgain, save })
</script>

<template>
  <OnboardingStack as="section" gap="xl" class="runtime-stage" aria-labelledby="runtime-title">
    <OnboardingStack>
      <h2 id="runtime-title">Choose a runtime and model</h2>
      <p class="quiet">Nothing connects or sends a model request here.</p>
    </OnboardingStack>

    <div v-if="!editing && current" class="set-row">
      <div class="set-text">
        <span class="set-label">Runtime and model</span>
        <span class="set-value">{{ PROVIDER_LABELS[current.provider] }} · <code>{{ current.model }}</code></span>
      </div>
      <OnboardingButton variant="link" class="change" @click="change">Change</OnboardingButton>
    </div>

    <template v-else>
      <OnboardingChoiceGroup label="Runtime" name="runtime" layout="segmented" :model-value="provider" @update:model-value="selectProvider">
        <OnboardingChoice value="claude">Claude Code</OnboardingChoice>
        <OnboardingChoice value="opencode">OpenCode</OnboardingChoice>
      </OnboardingChoiceGroup>
    </template>

    <OnboardingStack v-if="loadingStatus || error || connection" gap="sm" class="status">
      <p v-if="loadingStatus" role="status" class="quiet">Checking the runtime on this computer…</p>
      <p v-else-if="connection && signedIn" class="quiet">{{ providerLabel }} is ready on this computer.</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <div v-if="error && !loadingStatus"><OnboardingButton class="retry" @click="retry">Retry</OnboardingButton></div>
    </OnboardingStack>
    <p v-else-if="connections" class="error" role="alert">No status was reported for this runtime, so it cannot be confirmed ready.</p>

    <template v-if="connections && !connection && !loadingStatus">
      <div><OnboardingButton :disabled="loadingStatus" @click="checkAgain">Check again</OnboardingButton></div>
    </template>

    <template v-else-if="connection">
      <OnboardingStack v-if="!signedIn" class="repair">
        <p>{{ providerLabel }} is not ready on this computer. Install or sign in, then check again.</p>
        <p v-if="connection.command"><code>{{ connection.command }}</code></p>
        <div class="row">
          <HostTerminalButton v-if="terminalAction" :action="terminalAction" label="Open Terminal" />
          <OnboardingButton :disabled="loadingStatus" @click="checkAgain">Check again</OnboardingButton>
        </div>
      </OnboardingStack>

      <template v-if="editing">
        <OnboardingChoiceGroup v-if="signedIn && models !== null && reported.length" label="Models reported by this runtime" name="model" :model-value="selection" @update:model-value="choose">
          <OnboardingChoice v-for="model in reported" :key="model.id" :value="model.id">
            {{ model.label }}
            <template #description><code :title="model.id">{{ model.id }}</code> · reported by this runtime</template>
          </OnboardingChoice>
          <template #after><p class="quiet">A successful reply has not been verified.</p></template>
        </OnboardingChoiceGroup>
        <OnboardingChoiceGroup v-else-if="signedIn && models !== null && configured.length" label="Configured choices; availability not checked" name="model" :model-value="selection" @update:model-value="choose">
          <OnboardingChoice v-for="model in configured" :key="model" :value="model"><code>{{ model }}</code></OnboardingChoice>
          <template #after><p class="quiet">These configured aliases are not evidence that the account can use them.</p></template>
        </OnboardingChoiceGroup>
        <p v-else-if="signedIn && models !== null">There are no model choices reported or configured for this runtime.</p>
        <p v-else-if="signedIn && loadingModels" role="status" class="quiet">Loading models…</p>

        <p v-if="signedIn && models !== null" class="quiet">
          <a href="https://artificialanalysis.ai/" target="_blank" rel="noopener noreferrer">Artificial Analysis</a> ·
          <a href="https://arena.ai/leaderboard" target="_blank" rel="noopener noreferrer">Arena leaderboard</a> compare
          models, not account access or suitability.
        </p>

        <div class="row">
          <OnboardingButton class="defer" :disabled="saving" :aria-pressed="deferred" @click="defer">Decide later</OnboardingButton>
        </div>
        <p v-if="deferred" role="status" class="quiet">Deferred: this choice is recorded as not ready, not as connected.</p>
      </template>
    </template>
  </OnboardingStack>
</template>

<style scoped>
.set-row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-3, 12px); padding: var(--space-3, 12px) var(--space-4, 16px); border: 1px solid var(--border); border-radius: var(--radius-sm, 6px); background: var(--bg); }
.set-text { display: grid; gap: var(--space-1, 4px); min-width: 0; }
.set-label, .quiet { color: var(--fg2); }
.row { display: flex; flex-wrap: wrap; gap: var(--space-3, 12px); }
code { font: 12px var(--font-mono); color: var(--fg2); overflow-wrap: anywhere; }
.error { color: var(--warning); }
</style>
