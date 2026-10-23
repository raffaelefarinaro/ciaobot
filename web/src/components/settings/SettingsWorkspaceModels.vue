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
            :model-value="override('model') || WS_DEFAULT"
            :sections="modelSections"
            :labelled-by="`workspace-model-${form.name}`"
            :disabled="saving || !providerAvailable"
            @update:model-value="setField('model', $event, true)"
          />
        </div>
      </div>
      <p v-if="routines && !providerAvailable" class="hint hint--compact set-subrow-hint">
        Connect {{ providerLabel }} on Settings &gt; Providers to choose a model.
      </p>

      <label class="set-subrow">
        <span class="ws-label set-subrow-label">Thinking</span>
        <select
          :id="`ws-override-thinking-${provider}`"
          class="routine-select set-subrow-control"
          :value="override('thinking')"
          :disabled="saving || !models"
          @change="setField('thinking', ($event.target as HTMLSelectElement).value)"
        >
          <option value="">Engine default ({{ engineThinkingLabel }})</option>
          <option v-for="level in thinkingLevels" :key="level.value" :value="level.value">{{ capitalise(level.label) }}</option>
        </select>
      </label>
      <p v-if="!models" class="hint hint--compact set-subrow-hint">
        {{ modelsError ? 'Effort options unavailable.' : 'Loading effort options…' }}
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

      <div class="set-subrow">
        <span
          :id="`workspace-insights-model-${form.name}`"
          class="ws-label set-subrow-label"
          title="Reads archived chats, closed projects and task resolutions into memory, names new chats, and checks schedule results for this provider. Automatic uses the chat's own model; Claude schedule checks use Haiku."
        >Session insights</span>
        <div class="set-subrow-control">
          <ModelSelector
            :model-value="override('insights_model') || WS_DEFAULT"
            :sections="insightsSections"
            :labelled-by="`workspace-insights-model-${form.name}`"
            :disabled="saving || !providerAvailable"
            @update:model-value="setField('insights_model', $event, true)"
          />
        </div>
      </div>

      <p v-if="routinesError" class="hint hint--warn" role="alert">{{ routinesError }}</p>
      <p v-if="modelsError" class="hint hint--warn" role="alert">{{ modelsError }}</p>
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
</template>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'
import ModelSelector from '../ModelSelector.vue'
import { api } from '../../lib/api'
import { apiErrorMessage } from '../../lib/errorMessage'
import { modelDisplayName } from '../../lib/modelLabels'
import { setOverride } from '../../lib/workspaceOverrides'
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
const providerEls = ref<HTMLButtonElement[]>([])

const choices = useProviderChoices({ routines, workspaceModels: models })

const provider = computed<WorkspaceProvider>(() => form.value?.default_provider ?? 'claude')
const saving = computed(() => !!form.value && workspacesSaving.value === form.value.name)
const providerLabel = computed(
  () => workspaceProviderOptions.value.find((o) => o.value === provider.value)?.label || provider.value,
)
const providerAvailable = computed(() => !!choices.getProviderSection(provider.value)?.available)

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

const modelSections = computed(() =>
  choices.providerSectionsWithDefault(
    provider.value,
    WS_DEFAULT,
    (effective) => `Engine default (${effective ? modelDisplayName(effective) : 'automatic'})`,
    true,
  ),
)

const engineInsights = computed(() => routines.value?.provider_insights_models?.[provider.value] || '')
const insightsSections = computed(() =>
  choices.providerSectionsWithDefault(
    provider.value,
    WS_DEFAULT,
    () => `Engine default (${engineInsights.value ? modelDisplayName(engineInsights.value) : 'same as chat model'})`,
  ),
)

// The first entry is the composable's own "Automatic"; this card names the engine default instead.
const thinkingLevels = computed(() => choices.providerThinkingOptions(provider.value).slice(1))
const engineThinkingLabel = computed(() => {
  const level = routines.value?.provider_default_thinking?.[provider.value]
  return level ? capitalise(level) : 'Auto'
})

const modeOptions = PROVIDER_MODE_OPTIONS.filter((o) => o.value !== DEFAULT_MODE_SELECTION)
const engineModeLabel = computed(() => {
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
onMounted(async () => {
  const [r, m] = await Promise.allSettled([
    api.get<RoutineSettings>('/api/settings/routines'),
    api.get<ModelsResponse>('/api/models'),
  ])
  if (unmounted) return
  if (r.status === 'fulfilled') routines.value = r.value
  else routinesError.value = `Could not load the provider defaults: ${apiErrorMessage(r.reason, 'The request failed.')}`
  if (m.status === 'fulfilled') models.value = m.value
  else modelsError.value = `Could not load the model list: ${apiErrorMessage(m.reason, 'The request failed.')}`
})
onBeforeUnmount(() => { unmounted = true })
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.models-body .set-subrow,
.models-body .set-subrow-hint { padding-left: 0; }
.models-body .set-seg { flex: 0 1 auto; }
.models-hint { margin: var(--space-2) 0 0; }
</style>
