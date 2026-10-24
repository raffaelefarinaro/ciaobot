<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { apiErrorMessage } from '../../lib/errorMessage'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import { setOverride } from '../../lib/workspaceOverrides'

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState]; continue: []; back: []; refresh: [] }>()
const scope = ref('')
const savedScope = ref('')
const provider = ref('')
const overrides = ref<Record<string, Record<string, string>>>({})
const approval = ref('')
const savedApproval = ref('')
const platform = ref('')
const pending = ref(false)
const error = ref('')
const dirty = computed(() => scope.value !== savedScope.value || approval.value !== savedApproval.value)
const canContinue = computed(() => !!scope.value && !dirty.value && !pending.value && !error.value)
function report() { emit('state', { dirty: dirty.value, pending: pending.value, canContinue: canContinue.value, error: error.value || null }) }
watch([scope, approval, pending, error], report)

onMounted(async () => {
  try {
    const [res, status] = await Promise.all([
      api.get<{ workspaces?: Array<{ name: string; agent_fs_scope?: string | null; default_provider?: string; provider_overrides?: Record<string, Record<string, string>> }> }>('/api/workspaces'),
      api.get<Record<string, unknown>>('/api/status').catch((): Record<string, unknown> => ({})),
    ])
    const ws = res.workspaces?.find(item => item.name === props.context.workspace)
    scope.value = savedScope.value = ws?.agent_fs_scope === 'workspace' || ws?.agent_fs_scope === 'machine' ? ws.agent_fs_scope : ''
    provider.value = ws?.default_provider || ''
    overrides.value = ws?.provider_overrides || {}
    platform.value = String(status.platform || status.os || '').toLowerCase()
    const modes = (status as { provider_default_modes?: Record<string, string> }).provider_default_modes
    approval.value = savedApproval.value = modes?.[provider.value] || ''
  } catch (e) { error.value = `Could not load access settings: ${apiErrorMessage(e, 'The request failed.')}` }
  report()
})

async function save() {
  pending.value = true; error.value = ''; report()
  try {
    await api.patch(`/api/workspaces/${encodeURIComponent(props.context.workspace)}`, {
      ...(scope.value !== savedScope.value ? { agent_fs_scope: scope.value } : {}),
      ...(approval.value !== savedApproval.value ? { provider_overrides: setOverride(overrides.value, provider.value, 'permission_mode', approval.value) } : {}),
    })
    savedScope.value = scope.value; savedApproval.value = approval.value
    overrides.value = setOverride(overrides.value, provider.value, 'permission_mode', approval.value)
  } catch (e) { error.value = `Settings could not be saved: ${apiErrorMessage(e, 'The request failed.')}` }
  finally { pending.value = false; report() }
}
</script>

<template>
  <section class="access-stage" aria-labelledby="access-title">
    <h2 id="access-title">Choose what the agent can access</h2>
    <fieldset>
      <legend>Filesystem scope</legend>
      <p>Choose the files the agent and its shell may access.</p>
      <label><input v-model="scope" type="radio" value="workspace" :disabled="platform.includes('win')" /> Workspace only <span>Access stays inside this workspace.</span></label>
      <label><input v-model="scope" type="radio" value="machine" /> Whole machine <span>Access may include any file this user can read.</span></label>
      <p v-if="platform.includes('win')" class="hint">Windows has no filesystem sandbox; workspace-only scope is unavailable.</p>
      <p v-else class="hint">Scope limits file access; it is not a safety guarantee.</p>
    </fieldset>
    <fieldset>
      <legend>Approval mode</legend>
      <label>Runtime <select v-model="approval" aria-label="Approval mode" :disabled="!provider"><option value="">Unknown / engine default</option><option value="auto">Auto</option><option value="manual">Manual approvals</option><option value="bypass">Bypass permissions</option></select></label>
      <p v-if="provider === 'claude'">Recommended: Claude Auto. A classifier decides what is routine and what needs an approval card. This is not a safety guarantee.</p>
      <p v-else-if="provider === 'opencode'">OpenCode manual approvals use configurable rules. Bypass relies on instructions to the model; those instructions are not enforced safeguards.</p>
      <p v-else>Approval mode is unknown until the runtime is known.</p>
    </fieldset>
    <p v-if="error" role="alert" aria-live="assertive">{{ error }}</p>
    <button type="button" :disabled="pending || !dirty" @click="save">{{ pending ? 'Saving…' : 'Save choices' }}</button>
    <button type="button" :disabled="!canContinue || props.busy" @click="emit('continue')">Continue</button>
    <button type="button" @click="emit('back')">Back</button>
  </section>
</template>

<style scoped>
.access-stage { display: grid; gap: 16px; }
fieldset { display: grid; gap: 8px; min-width: 0; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius, 10px); }
legend { font-weight: 700; }
label { display: flex; align-items: center; gap: 8px; min-height: 44px; }
label span { color: var(--fg2); }
button, select { min-height: 44px; }
</style>
