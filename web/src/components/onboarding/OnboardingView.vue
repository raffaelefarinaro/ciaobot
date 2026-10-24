<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../../lib/api'
import { useRoute, useRouter } from 'vue-router'
import OnboardingShell from './OnboardingShell.vue'
import OnboardingLocationStage from './OnboardingLocationStage.vue'
import OnboardingRuntimeStage from './OnboardingRuntimeStage.vue'
import OnboardingAccessStage from './OnboardingAccessStage.vue'
import OnboardingMemoryStage from './OnboardingMemoryStage.vue'
import OnboardingCareStage from './OnboardingCareStage.vue'
import OnboardingFinishStage from './OnboardingFinishStage.vue'
import { useOnboardingStore } from '../../stores/onboarding'
import type { OnboardingStageContext, OnboardingStep, OnboardingTarget, StageState } from '../../lib/onboarding'

const route = useRoute(), router = useRouter(), store = useOnboardingStore()
const target: OnboardingTarget = route.query.workspace ? { kind: 'workspace', name: String(route.query.workspace) } : { kind: 'installation' }
const context = ref<OnboardingStageContext>({ target, workspace: String(route.query.workspace || ''), revision: 0, provisioned: false })
const step = ref<OnboardingStep>('location')
const state = ref<StageState>({ dirty: false, pending: false, canContinue: false, error: null })
// Access and memory share one screen, so both must be ready before advancing.
const accessState = ref<StageState>({ dirty: false, pending: false, canContinue: false, error: null })
const memoryState = ref<StageState>({ dirty: false, pending: false, canContinue: false, error: null })
const combinedReady = computed(() =>
  accessState.value.canContinue && memoryState.value.canContinue &&
  !accessState.value.pending && !memoryState.value.pending &&
  !accessState.value.error && !memoryState.value.error,
)
const busy = computed(() => store.loading || store.saving)
const stageProps = computed(() => ({ context: context.value, busy: busy.value }))
onMounted(async () => {
  await store.load()
  // The stages need the workspace name and the provisioned fact; a target that
  // names a workspace keeps it, an installation uses the primary one.
  const [workspaces, setup] = await Promise.allSettled([
    api.get<{ workspaces?: Array<{ name: string }> }>('/api/workspaces'),
    api.get<{ workspace_root?: string }>('/api/setup-status'),
  ])
  const names = workspaces.status === 'fulfilled' ? workspaces.value.workspaces?.map(item => item.name) ?? [] : []
  context.value.workspace = target.kind === 'workspace' ? target.name : names[0] ?? ''
  context.value.provisioned = setup.status === 'fulfilled' && !!setup.value.workspace_root
  const p = store.progress(target)
  if (p) { step.value = p.step; context.value.revision = store.receipt?.revision ?? 0 }
})
function update(value: StageState) { state.value = value }
function updateAccess(value: StageState) { accessState.value = value; syncCombined() }
function updateMemory(value: StageState) { memoryState.value = value; syncCombined() }
function syncCombined() {
  state.value = { dirty: accessState.value.dirty || memoryState.value.dirty, pending: accessState.value.pending || memoryState.value.pending, canContinue: combinedReady.value, error: accessState.value.error || memoryState.value.error }
}
const shellContinue = computed(() => (step.value === 'access_memory' ? combinedReady.value : state.value.canContinue))
async function advance() {
  if (!shellContinue.value || busy.value) return
  const order: OnboardingStep[] = ['location','runtime','access_memory','care','finish']
  if (step.value === 'finish') { if (await store.complete(target)) await router.replace(validReturn()) ; return }
  const next = order[order.indexOf(step.value) + 1]!
  if (await store.setStep(target, next)) { step.value = next; context.value.revision = store.receipt?.revision ?? 0 }
}
function validReturn() { const value = String(route.query.returnTo || '/'); return value.startsWith('/') && !value.startsWith('//') && !value.includes('\\') && !/^\/(login|onboarding)(?:[/?#]|$)/.test(value) && !/[?&](?:setup_token|token)=/i.test(value) ? value : '/' }
function back() { const steps: OnboardingStep[] = ['location','runtime','access_memory','care','finish']; const i = steps.indexOf(step.value); if (i) step.value = steps[i - 1]! }
function leave() { router.replace(validReturn()) }
async function reload() { await store.load(); const p = store.progress(target); if (p) step.value = p.step }
</script>
<template>
  <OnboardingShell :context="context" :step="step" :busy="busy" :error="store.error" :conflict="store.conflict" :can-continue="shellContinue" @continue="advance" @back="back" @leave="leave" @reload="reload">
    <template #stage>
      <OnboardingLocationStage v-if="step==='location'" v-bind="stageProps" @state="update" />
      <OnboardingRuntimeStage v-else-if="step==='runtime'" v-bind="stageProps" @state="update" />
      <template v-else-if="step==='access_memory'"><OnboardingAccessStage v-bind="stageProps" @state="updateAccess"/><OnboardingMemoryStage v-bind="stageProps" @state="updateMemory"/></template>
      <OnboardingCareStage v-else-if="step==='care'" v-bind="stageProps" @state="update" />
      <OnboardingFinishStage v-else v-bind="stageProps" @state="update" />
    </template>
  </OnboardingShell>
</template>
