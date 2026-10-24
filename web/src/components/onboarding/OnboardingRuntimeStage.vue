<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../../lib/api'
import type { HostTerminalAction, ModelsResponse, RuntimeProvider, WorkspaceInfo } from '../../lib/types'
import { apiErrorMessage } from '../../lib/errorMessage'
import { normalizeOverrides, setOverride } from '../../lib/workspaceOverrides'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import HostTerminalButton from '../HostTerminalButton.vue'

interface ProviderStatus {
  ok?: boolean
  auth?: string
  command?: string
  terminal_action?: HostTerminalAction
  label?: string
}

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState]; continue: []; back: []; refresh: [] }>()

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
const state = computed<StageState>(() => ({
  dirty: false,
  pending: loadingStatus.value || loadingModels.value || saving.value,
  canContinue: !saving.value && ready.value && (!!selection.value || deferred.value),
  error: error.value || null,
}))

watch(state, value => emit('state', value), { immediate: true })

function resetChoice(): void {
  saveError.value = ''
  selection.value = ''
  deferred.value = false
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

// Models load only on an explicit action: on a cache miss the engine may start
// an ephemeral provider process, which a page mount must never do.
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
  emit('refresh')
}

function choose(id: string): void {
  saveError.value = ''
  selection.value = id
  deferred.value = false
}

function defer(): void {
  saveError.value = ''
  selection.value = ''
  deferred.value = true
}

watch(provider, resetChoice)

async function save(): Promise<boolean> {
  if (saving.value || loadingStatus.value || loadingModels.value || !connection.value || statusError.value || modelsError.value) return false
  if (deferred.value) return true
  if (!selection.value) return false
  const chosenProvider = provider.value
  const chosenModel = selection.value
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
    return true
  } catch (reason) {
    saveError.value = apiErrorMessage(reason, 'The runtime and model could not be saved.')
    return false
  } finally {
    saving.value = false
  }
}

defineExpose({ refresh: checkAgain, save })
</script>

<template>
  <section class="runtime-stage" aria-labelledby="runtime-title">
    <h2 id="runtime-title">Choose a runtime and model</h2>
    <p>Select the runtime first, then choose an explicit model. Nothing connects or sends a model request here.</p>

    <fieldset :disabled="saving">
      <legend>Runtime</legend>
      <label><input v-model="provider" type="radio" value="claude"> Claude Code</label>
      <label><input v-model="provider" type="radio" value="opencode"> OpenCode</label>
    </fieldset>

    <p v-if="loadingStatus" role="status">Checking the runtime on this computer…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button v-if="error && !loadingStatus" type="button" class="retry" @click="retry">Retry</button>

    <div v-if="!connections" class="models-empty">
      <p>Checking a runtime reads its status on this computer; it may also list its connectors.</p>
      <button type="button" :disabled="loadingStatus" @click="checkAgain">
        {{ loadingStatus ? 'Checking…' : 'Check this computer' }}
      </button>
    </div>

    <template v-else-if="connection">
      <div v-if="!signedIn" class="repair">
        <p>{{ connection?.label ?? provider }} is not ready on this computer. Install or sign in, then check again.</p>
        <p v-if="connection?.command"><code>{{ connection.command }}</code></p>
        <HostTerminalButton v-if="terminalAction" :action="terminalAction" label="Open Terminal" />
        <button type="button" :disabled="loadingStatus" @click="checkAgain">Check again</button>
      </div>
      <div v-else-if="models === null" class="models-empty">
        <p>Model choices are not loaded yet. Loading them may start the runtime briefly.</p>
        <button type="button" :disabled="loadingModels" @click="loadModels">
          {{ loadingModels ? 'Loading…' : 'Load available models' }}
        </button>
      </div>
      <template v-else>
        <fieldset v-if="reported.length" :disabled="saving">
          <legend>Models reported by this runtime</legend>
          <label v-for="model in reported" :key="model.id" class="choice">
            <input type="radio" name="model" :checked="selection === model.id" @change="choose(model.id)">
            {{ model.label }} <code :title="model.id">{{ model.id }}</code>
          </label>
          <p>A successful reply has not been verified.</p>
        </fieldset>
        <fieldset v-else-if="configured.length" :disabled="saving">
          <legend>Configured choices; availability not checked</legend>
          <label v-for="model in configured" :key="model" class="choice">
            <input type="radio" name="model" :checked="selection === model" @change="choose(model)">
            <code>{{ model }}</code>
          </label>
          <p>These configured aliases are not evidence that the account can use them.</p>
        </fieldset>
        <div v-else>
          <p>There are no model choices reported or configured for this runtime.</p>
        </div>
        <p>
          <a href="https://artificialanalysis.ai/" target="_blank" rel="noopener noreferrer">Artificial Analysis</a> ·
          <a href="https://arena.ai/leaderboard" target="_blank" rel="noopener noreferrer">Arena leaderboard</a> compare
          models, not account access or suitability.
        </p>
      </template>

      <button type="button" class="defer" :disabled="saving" :aria-pressed="deferred" @click="defer">Decide later</button>
      <p v-if="deferred" role="status">Deferred: this choice is recorded as not ready, not as connected.</p>
    </template>
    <div v-else class="models-empty">
      <p role="alert">No status was reported for this runtime, so it cannot be confirmed ready.</p>
      <button type="button" :disabled="loadingStatus" @click="checkAgain">Check again</button>
    </div>
  </section>
</template>

<style scoped>
.runtime-stage { display: grid; gap: 12px; }
fieldset { border: 1px solid var(--border); border-radius: var(--radius-sm, 6px); padding: 8px 12px; }
legend { padding: 0 4px; color: var(--fg2); }
.choice { display: flex; align-items: center; gap: 8px; min-height: 44px; }
button { min-height: 44px; padding: 8px 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: transparent; color: var(--fg); font: inherit; cursor: pointer; }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button:disabled { opacity: .55; cursor: not-allowed; }
code { font: 12px var(--font-mono); color: var(--fg2); overflow-wrap: anywhere; }
.error { color: var(--warning); }
.repair, .models-empty { display: grid; gap: 8px; }
</style>
