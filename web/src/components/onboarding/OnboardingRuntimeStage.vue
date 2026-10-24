<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import type { ModelsResponse, RuntimeProvider } from '../../lib/types'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import HostTerminalButton from '../HostTerminalButton.vue'

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState]; continue: []; back: []; refresh: [] }>()
const response = ref<ModelsResponse | null>(null)
const loading = ref(false)
const error = ref('')
const provider = ref<RuntimeProvider>('claude')
const selection = ref('')
const deferred = ref(false)
const evidence = computed(() => response.value?.provider_catalog_evidence?.[provider.value])
const reported = computed(() => evidence.value?.status === 'observed' ? evidence.value.models : [])
const configured = computed(() => response.value?.provider_models?.[provider.value] ?? [])
const state = computed(() => ({ dirty: false, pending: loading.value, canContinue: !!selection.value || deferred.value, error: error.value || null }))
watch(state, value => emit('state', value), { immediate: true })
watch(provider, () => { selection.value = ''; deferred.value = false })

async function load() {
  loading.value = true; error.value = ''
  try { response.value = await api.get<ModelsResponse>(`/api/models?workspace=${encodeURIComponent(props.context.workspace)}`) }
  catch { error.value = 'Could not load model information. Check again or defer this choice.' }
  finally { loading.value = false }
}
async function checkAgain() {
  try { await api.post(`/api/providers/${provider.value}/verify`, {}) } catch { /* evidence refresh below remains the source of truth */ }
  await load()
}
function choose(id: string) { selection.value = id; deferred.value = false }
function defer() { selection.value = ''; deferred.value = true }
onMounted(load)
</script>

<template>
  <section class="runtime-stage" aria-labelledby="runtime-title">
    <h2 id="runtime-title">Choose a runtime and model</h2>
    <p>Select the runtime first, then choose an explicit model. Nothing connects or sends a model request here.</p>
    <fieldset><legend>Runtime</legend>
      <label><input v-model="provider" type="radio" value="claude"> Claude Code</label>
      <label><input v-model="provider" type="radio" value="opencode"> OpenCode</label>
    </fieldset>
    <p v-if="loading" role="status">Loading model information…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="response">
      <fieldset v-if="reported.length"><legend>Models reported by this runtime</legend>
        <label v-for="model in reported" :key="model.id" class="choice"><input type="radio" name="model" :checked="selection === model.id" @change="choose(model.id)"> {{ model.label }} <code :title="model.id">{{ model.id }}</code></label>
        <p>A successful reply has not been verified.</p>
      </fieldset>
      <fieldset v-else-if="configured.length"><legend>Configured choices; availability not checked</legend>
        <label v-for="model in configured" :key="model" class="choice"><input type="radio" name="model" :checked="selection === model" @change="choose(model)"> <code>{{ model }}</code></label>
        <p>These configured aliases are not evidence that the account can use them.</p>
      </fieldset>
      <div v-else>
        <p>There are no model choices reported or configured for this runtime.</p>
        <p>Install or sign in on the engine computer, then check again.</p>
        <HostTerminalButton :action="provider === 'claude' ? 'claude-login' : 'opencode-login'" label="Sign in in Terminal" />
        <code class="command">{{ provider === 'claude' ? 'claude' : 'opencode auth login' }}</code>
        <button type="button" class="btn-small" @click="checkAgain">Check again</button>
      </div>
    </template>
    <button type="button" class="btn-small" :aria-pressed="deferred" @click="defer">{{ deferred ? 'Deferred for now' : 'Decide later' }}</button>
    <nav aria-label="Model comparison"><a href="https://artificialanalysis.ai/" target="_blank" rel="noopener noreferrer">Artificial Analysis</a> · <a href="https://arena.ai/leaderboard" target="_blank" rel="noopener noreferrer">Arena leaderboard</a><p>These compare models, not account access or suitability.</p></nav>
  </section>
</template>

<style scoped>
.runtime-stage { display: grid; gap: var(--space-3); max-width: 680px; }
fieldset { display: grid; gap: var(--space-2); border: 0; border-top: 1px solid var(--border); padding: var(--space-3) 0 0; }
legend { font-weight: 600; padding: 0 var(--space-1) 0 0; }
label { display: flex; align-items: center; gap: var(--space-2); min-height: 44px; }
.choice code, .command { overflow-wrap: anywhere; }
nav p, fieldset p { color: var(--fg2); margin: 0; }
@media (pointer: coarse) { .runtime-stage button { min-height: var(--touch); } }
</style>
