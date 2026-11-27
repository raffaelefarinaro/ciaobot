<template>
  <div v-if="form" class="card workspace-models">
    <div class="settings-card-header">
      <p class="section-title">Chats and models</p>
      <p class="hint">
        Defaults for new chats and schedules in this workspace. Leave a field on Engine default to use
        what Settings &gt; Providers sets.
      </p>
    </div>
    <div class="models-body">
      <div class="set-subrow">
        <span :id="`workspace-provider-${form.name}`" class="ws-label set-subrow-label">Default provider</span>
        <div
          class="set-seg set-subrow-control"
          role="radiogroup"
          :aria-labelledby="`workspace-provider-${form.name}`"
          @keydown="onProviderKeydown"
        >
          <button
            v-for="option in workspaceProviderOptions"
            :key="option.value"
            ref="providerEls"
            type="button"
            role="radio"
            :aria-checked="provider === option.value"
            :tabindex="provider === option.value ? 0 : -1"
            :disabled="saving"
            @click="chooseProvider(option.value)"
          >{{ option.label }}</button>
        </div>
      </div>

      <div class="set-subrow">
        <span :id="`workspace-model-${form.name}`" class="ws-label set-subrow-label">Default model</span>
        <div class="set-subrow-control">
          <ModelSelector
            :key="`${generation}:${providerGeneration}`"
            :model-value="override('model') || WS_DEFAULT"
            :sections="modelSections"
            :labelled-by="`workspace-model-${form.name}`"
            :disabled="saving || !providerAvailable"
            @update:model-value="modelSelection"
          />
        </div>
      </div>
      <p v-if="models && !providerAvailable" class="hint hint--compact set-subrow-hint">
        No models reported; availability unknown.
      </p>
      <p ref="modelStatus" class="hint hint--compact set-subrow-hint" role="status" tabindex="-1">
        {{ !form.name ? 'Workspace unavailable; model options cannot be loaded.' : modelsLoading ? 'Loading model options…' : modelsError ? 'Model options unavailable.' : models ? 'Model options loaded.' : 'Model options unavailable.' }}
      </p>

      <label class="set-subrow">
        <span class="ws-label set-subrow-label">Thinking</span>
        <select
          :key="`${generation}:${providerGeneration}`"
          :id="`ws-override-thinking-${provider}`"
          :data-generation="generation"
          :data-provider-generation="providerGeneration"
          :data-workspace="form.name"
          :data-provider="provider"
          class="routine-select set-subrow-control"
          :value="override('thinking')"
          :disabled="saving || !models"
          @change="thinkingChange"
        >
          <option value="">Engine default ({{ engineThinkingLabel }})</option>
          <option v-for="level in thinkingLevels" :key="level.value" :value="level.value">{{ capitalise(level.label) }}</option>
        </select>
      </label>
      <p v-if="!models" class="hint hint--compact set-subrow-hint">
        {{ modelsError || !form.name ? 'Effort options unavailable.' : 'Loading effort options…' }}
      </p>

      <label class="set-subrow">
        <span class="ws-label set-subrow-label">Permission mode</span>
        <select
          :id="`ws-override-permission_mode-${provider}`"
          class="routine-select set-subrow-control"
          :value="override('permission_mode')"
          :disabled="saving"
          @change="setField('permission_mode', ($event.target as HTMLSelectElement).value)"
        >
          <option value="">Engine default ({{ engineModeLabel }})</option>
          <option v-for="mode in modeOptions" :key="mode.value" :value="mode.value">{{ mode.label }}</option>
        </select>
      </label>
      <p class="hint hint--compact set-subrow-hint" data-testid="permission-note">
        {{ BYPASS_NOTE }}
        <a v-if="permissionDocs" :href="permissionDocs.href" target="_blank" rel="noopener">{{ permissionDocs.label }}</a>
      </p>

      <div class="set-subrow">
        <span
          :id="`workspace-insights-model-${form.name}`"
          class="ws-label set-subrow-label"
          title="Reads archived chats, closed projects and task resolutions into memory, names new chats, and checks schedule results for this provider. Automatic uses the chat's own model; Claude schedule checks use Haiku."
        >Session insights</span>
        <div class="set-subrow-control">
          <ModelSelector
            :key="`${generation}:${providerGeneration}`"
            :model-value="override('insights_model') || WS_DEFAULT"
            :sections="insightsSections"
            :labelled-by="`workspace-insights-model-${form.name}`"
            :disabled="saving || !providerAvailable"
            @update:model-value="insightsSelection"
          />
        </div>
      </div>

      <p v-if="routinesError" class="hint hint--warn" role="alert">{{ routinesError }}</p>
      <p v-if="modelsError" class="hint hint--warn" role="alert">{{ modelsError }}</p>
      <button v-if="showRetry" ref="retryButton" type="button" class="btn-small" :aria-disabled="modelsLoading" @click="retryModels">Retry</button>
      <p class="hint hint--compact models-hint">
        Switching the provider shows that provider's overrides; each provider keeps its own.
      </p>
      <p v-if="workspaceDirty(form)" class="hint hint--compact">Save changes applies to every card above it.</p>
      <div v-if="workspaceDirty(form)" class="workspace-edit-actions">
        <button
          type="button"
          class="btn-primary btn-small workspace-save"
          :disabled="workspacesSaving === form.name || archivingWorkspace === form.name"
          @click="saveWorkspace(form.name)"
        >{{ workspacesSaving === form.name ? 'Saving...' : 'Save changes' }}</button>
        <button
          type="button"
          class="btn-small workspace-discard"
          :disabled="workspacesSaving === form.name"
          @click="discardWorkspace(form.name)"
        >Discard</button>
      </div>
    </div>
  </div>
  <p v-else role="status" class="hint">Workspace unavailable; model options cannot be loaded.</p>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ModelSelector from '../ModelSelector.vue'
import { api } from '../../lib/api'
import { providerShortLabel } from '../../lib/modelSections'
import { setOverride } from '../../lib/workspaceOverrides'
import { BYPASS_NOTE, permissionDocsFor } from '../../lib/permissionDocs'
import { DEFAULT_MODE_SELECTION, PROVIDER_MODE_OPTIONS, useProviderChoices } from '../../composables/useProviderChoices'
import { WORKSPACE_ADMIN_KEY } from '../../composables/useWorkspaceAdmin'
import type { ModelsResponse, ProviderOverrideField, RoutineSettings, WorkspaceProvider } from '../../lib/types'

// Workspace > Overview > "Chats and models": this workspace's overrides of the
// Settings > Providers defaults. It edits the same form object as the rest of
// the page. This is the last form card, so it carries the page's one Save
// changes / Discard row; the unsaved-draft confirm needs no code here. Empty always means "engine default"; the first option of every
// field names that engine value.
const admin = inject(WORKSPACE_ADMIN_KEY)
if (!admin) throw new Error('SettingsWorkspaceModels needs the workspace admin SettingsView provides')

const {
  activeWorkspaceForm: form,
  workspaceProviderOptions,
  workspacesSaving,
  archivingWorkspace,
  workspaceDirty,
  saveWorkspace,
  discardWorkspace,
} = admin

const WS_DEFAULT = '__ciao_ws_default__'

const routines = ref<RoutineSettings | null>(null)
const models = ref<ModelsResponse | null>(null)
const routinesError = ref('')
const modelsError = ref('')
const modelsLoading = ref(false)
const showRetry = ref(false)
const retryButton = ref<HTMLButtonElement | null>(null)
const modelStatus = ref<HTMLElement | null>(null)
const generation = ref(0)
const providerEls = ref<HTMLButtonElement[]>([])

const choices = useProviderChoices({ routines, workspaceModels: models })

const provider = computed<WorkspaceProvider>(() => form.value?.default_provider ?? 'claude')
const permissionDocs = computed(() => permissionDocsFor(provider.value))
const providerGeneration = ref(0)
watch(provider, () => { providerGeneration.value++ }, { flush: 'sync' })
const saving = computed(() => !!form.value && workspacesSaving.value === form.value.name)
const reportedModels = computed(() => provider.value === 'opencode'
  ? models.value?.opencode_models?.length ? models.value.opencode_models : models.value?.provider_models?.opencode || []
  : models.value?.provider_models?.claude || models.value?.models || [])
const providerAvailable = computed(() => reportedModels.value.length > 0)

function override(field: ProviderOverrideField): string {
  return form.value?.provider_overrides[provider.value]?.[field] ?? ''
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function setField(field: ProviderOverrideField, raw: string | string[], sentinel = false): void {
  if (!form.value) return
  const picked = Array.isArray(raw) ? raw[0] || '' : raw
  const value = sentinel && picked === WS_DEFAULT ? '' : picked
  form.value.provider_overrides = setOverride(form.value.provider_overrides, provider.value, field, value)
}

function selection(field: ProviderOverrideField) {
  const name = form.value?.name
  const current = generation.value
  const currentProviderGeneration = providerGeneration.value
  const selectedProvider = provider.value
  return (value: string | string[]) => {
    if (generation.value === current && providerGeneration.value === currentProviderGeneration && form.value?.name === name && provider.value === selectedProvider && models.value && providerAvailable.value) {
      setField(field, value, true)
    }
  }
}
const modelSelection = computed(() => selection('model'))
const insightsSelection = computed(() => selection('insights_model'))
function thinkingChange(event: Event): void {
  const select = event.currentTarget as HTMLSelectElement
  if (select.dataset.generation === String(generation.value) && select.dataset.providerGeneration === String(providerGeneration.value) &&
      select.dataset.workspace === form.value?.name &&
      select.dataset.provider === provider.value && models.value && !saving.value) {
    setField('thinking', select.value)
  }
}

function sections(insights: boolean) {
  if (!routines.value) {
    const options = reportedModels.value
    return [
      { key: 'default', label: '', models: [WS_DEFAULT], modelLabels: { [WS_DEFAULT]: 'Engine default (unknown)' } },
      { key: provider.value, label: providerShortLabel(models.value, provider.value), models: options, modelLabels: choices.modelLabelsFor(options, provider.value), modelBadges: choices.modelBadgesFor(options) },
    ]
  }
  const result = choices.providerSectionsWithDefault(
    provider.value,
    WS_DEFAULT,
    (effective) => insights
      ? `Engine default (${engineInsights.value ? choices.modelNameFor(provider.value, engineInsights.value) : 'same as chat model'})`
      : `Engine default (${effective ? choices.modelNameFor(provider.value, effective) : 'automatic'})`,
    !insights,
  )
  result[1] = choices.withoutAgentDefault({
    ...result[1],
    models: reportedModels.value,
    modelLabels: choices.modelLabelsFor(reportedModels.value, provider.value),
    modelBadges: choices.modelBadgesFor(reportedModels.value),
    disabled: !providerAvailable.value,
  }, provider.value, WS_DEFAULT)
  return result
}
const modelSections = computed(() => sections(false))

const engineInsights = computed(() => routines.value?.provider_insights_models?.[provider.value] || '')
const insightsSections = computed(() => sections(true))

// The first entry is the composable's own "Automatic"; this card names the engine default instead.
const thinkingLevels = computed(() => choices.providerThinkingOptions(provider.value).slice(1))
const engineThinkingLabel = computed(() => {
  if (!routines.value) return 'unknown'
  const level = routines.value?.provider_default_thinking?.[provider.value]
  return level ? capitalise(level) : 'Auto'
})

const modeOptions = PROVIDER_MODE_OPTIONS.filter((o) => o.value !== DEFAULT_MODE_SELECTION)
const engineModeLabel = computed(() => {
  if (!routines.value) return 'unknown'
  const mode = routines.value?.provider_default_modes?.[provider.value] || 'auto'
  return capitalise(mode)
})

function chooseProvider(value: WorkspaceProvider): void {
  if (form.value && !saving.value) form.value.default_provider = value
}

// Radio-group keys: arrows select the neighbour (wrapping), Home/End the ends.
function onProviderKeydown(e: KeyboardEvent): void {
  const options = workspaceProviderOptions.value
  if (!options.length || saving.value) return
  const at = options.findIndex((o) => o.value === provider.value)
  let target: number
  switch (e.key) {
    case 'ArrowRight':
    case 'ArrowDown':
      target = (at + 1) % options.length
      break
    case 'ArrowLeft':
    case 'ArrowUp':
      target = (at <= 0 ? options.length : at) - 1
      break
    case 'Home':
      target = 0
      break
    case 'End':
      target = options.length - 1
      break
    default:
      return
  }
  e.preventDefault()
  providerEls.value[target]?.focus()
  chooseProvider(options[target].value)
}

let unmounted = false
async function fetchModels(): Promise<void> {
  const name = form.value?.name
  const current = ++generation.value
  models.value = null
  modelsError.value = ''
  modelsLoading.value = !!name
  if (!name) return
  const valid = () => !unmounted && generation.value === current && form.value?.name === name
  try {
    const result = await api.get<ModelsResponse>(`/api/models?${new URLSearchParams({ workspace: name })}`)
    if (valid()) {
      const restoreFocus = document.activeElement === retryButton.value && retryButton.value !== null
      models.value = result
      modelsLoading.value = false
      showRetry.value = false
      if (restoreFocus) {
        await nextTick()
        if (valid() && document.activeElement === document.body) modelStatus.value?.focus()
      }
    }
  } catch {
    if (valid()) {
      modelsLoading.value = false
      modelsError.value = 'Could not load the model list.'
      showRetry.value = true
    }
  }
}
function retryModels(): void {
  if (!modelsLoading.value) void fetchModels()
}
watch(() => form.value?.name, () => { showRetry.value = false; void fetchModels() }, { immediate: true, flush: 'sync' })
onMounted(async () => {
  try {
    const result = await api.get<RoutineSettings>('/api/settings/routines')
    if (!unmounted) routines.value = result
  } catch {
    if (!unmounted) routinesError.value = 'Could not load the provider defaults.'
  }
})
onBeforeUnmount(() => { unmounted = true; generation.value++ })
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.models-body .set-subrow,
.models-body .set-subrow-hint { padding-left: 0; }
.models-body .set-seg { flex: 0 1 auto; }
.models-hint { margin: var(--space-2) 0 0; }
</style>
