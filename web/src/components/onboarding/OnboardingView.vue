<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../../lib/api'
import OnboardingShell from './OnboardingShell.vue'
import OnboardingLocationStage from './OnboardingLocationStage.vue'
import OnboardingRuntimeStage from './OnboardingRuntimeStage.vue'
import OnboardingAccessStage from './OnboardingAccessStage.vue'
import OnboardingMemoryStage from './OnboardingMemoryStage.vue'
import OnboardingCareStage from './OnboardingCareStage.vue'
import OnboardingFinishStage from './OnboardingFinishStage.vue'
import { useOnboardingStore } from '../../stores/onboarding'
import type {
  OnboardingStageContext,
  OnboardingStep,
  OnboardingTarget,
  StageState,
} from '../../lib/onboarding'

const route = useRoute()
const router = useRouter()
const store = useOnboardingStore()

const target = ref<OnboardingTarget>(
  route.query.workspace
    ? { kind: 'workspace', name: String(route.query.workspace) }
    : { kind: 'installation' },
)
const context = ref<OnboardingStageContext>({
  target: target.value,
  workspace: '',
  revision: 0,
  provisioned: false,
})
const step = ref<OnboardingStep>('location')
const state = ref<StageState>({ dirty: false, pending: false, canContinue: false, error: null })
const accessState = ref<StageState>({ dirty: false, pending: false, canContinue: false, error: null })
const memoryState = ref<StageState>({ dirty: false, pending: false, canContinue: false, error: null })
// Nothing may mount until the target, the workspace name and the provisioned
// fact are resolved; otherwise Location initialises against an empty context and
// stays read-only in provisioned installs.
const contextReady = ref(false)
const targetError = ref('')
const busy = computed(() => store.loading || store.saving)
const stageProps = computed(() => ({ context: context.value, busy: busy.value }))
const combinedReady = computed(() =>
  accessState.value.canContinue && memoryState.value.canContinue &&
  !accessState.value.pending && !memoryState.value.pending &&
  !accessState.value.error && !memoryState.value.error,
)
// On the shared screen the Memory stage owns the save, so the shell's own
// Continue stays out of the way and memory advances after its save succeeds.
const shellContinue = computed(() =>
  step.value === 'access_memory' ? false : state.value.canContinue,
)

function update(value: StageState) { state.value = value }
function updateAccess(value: StageState) { accessState.value = value }
function updateMemory(value: StageState) { memoryState.value = value }

async function load(): Promise<void> {
  await store.load()
  const [workspaces, setup] = await Promise.allSettled([
    api.get<{ workspaces?: Array<{ name: string }> }>('/api/workspaces'),
    api.get<{ workspace_root?: string }>('/api/setup-status'),
  ])
  const names = workspaces.status === 'fulfilled'
    ? workspaces.value.workspaces?.map(item => item.name) ?? []
    : []
  const progress = store.progress(target.value)
  if (target.value.kind === 'workspace') {
    // A workspace target must be registered and already tracked by the receipt;
    // a stale link is an error, never an installation journey.
    if (!names.includes(target.value.name) || !progress) {
      targetError.value = `Workspace “${target.value.name}” is not available for onboarding.`
      return
    }
    context.value.workspace = target.value.name
  } else {
    context.value.workspace = names[0] ?? ''
  }
  context.value.provisioned = setup.status === 'fulfilled' && !!setup.value.workspace_root
  if (progress) step.value = progress.step
  context.value.revision = store.receipt?.revision ?? 0
  contextReady.value = true
}

onMounted(load)

async function advance(): Promise<void> {
  // On the shared screen readiness lives in the two stage states; the shell's
  // own flag is deliberately off there so Memory's save owns the action.
  const ready = step.value === 'access_memory' ? combinedReady.value : state.value.canContinue
  if (!ready || busy.value || !contextReady.value) return
  const order: OnboardingStep[] = ['location', 'runtime', 'access_memory', 'care', 'finish']
  if (step.value === 'finish') {
    if (await store.complete(target.value)) await router.replace(validReturn())
    return
  }
  const next = order[order.indexOf(step.value) + 1]!
  if (await store.setStep(target.value, next)) {
    step.value = next
    context.value.revision = store.receipt?.revision ?? 0
  }
}

// Memory emits `continue` only after its own setting is saved; advance only
// when the access half is also ready.
async function afterMemorySave(): Promise<void> {
  if (!combinedReady.value) return
  await advance()
}

function validReturn(): string {
  const value = String(route.query.returnTo || '/')
  const internal = value.startsWith('/') && !value.startsWith('//') && !value.includes('\\')
  const notAuth = !/^\/(login|onboarding)(?:[/?#]|$)/.test(value)
  const noToken = !/[?&](?:setup_token|token)=/i.test(value)
  return internal && notAuth && noToken ? value : '/'
}

function back(): void {
  const order: OnboardingStep[] = ['location', 'runtime', 'access_memory', 'care', 'finish']
  const index = order.indexOf(step.value)
  if (index) step.value = order[index - 1]!
}

function leave(): void { void router.replace(validReturn()) }

async function reload(): Promise<void> {
  targetError.value = ''
  await load()
  const progress = store.progress(target.value)
  if (progress) step.value = progress.step
}
</script>

<template>
  <section v-if="targetError" class="onboarding-error" role="alert">
    <p>{{ targetError }}</p>
    <button type="button" @click="router.replace('/')">Back to Ciaobot</button>
  </section>
  <p v-else-if="store.error" class="onboarding-error" role="alert">
    {{ store.error }}
    <button type="button" @click="reload">Retry</button>
  </p>
  <OnboardingShell
    v-else-if="contextReady"
    :context="context"
    :step="step"
    :busy="busy"
    :error="state.error || accessState.error || memoryState.error"
    :conflict="store.conflict"
    :can-continue="shellContinue"
    @continue="advance"
    @back="back"
    @leave="leave"
    @reload="reload"
  >
    <template #stage>
      <OnboardingLocationStage v-if="step === 'location'" v-bind="stageProps" @state="update" />
      <OnboardingRuntimeStage v-else-if="step === 'runtime'" v-bind="stageProps" @state="update" />
      <template v-else-if="step === 'access_memory'">
        <OnboardingAccessStage v-bind="stageProps" @state="updateAccess" />
        <OnboardingMemoryStage v-bind="stageProps" @state="updateMemory" @continue="afterMemorySave" />
      </template>
      <OnboardingCareStage v-else-if="step === 'care'" v-bind="stageProps" @state="update" />
      <OnboardingFinishStage v-else v-bind="stageProps" @state="update" />
    </template>
  </OnboardingShell>
  <p v-else class="onboarding-loading" role="status">Loading setup…</p>
</template>

<style scoped>
.onboarding-error, .onboarding-loading { max-width: 760px; margin: 24px auto; padding: 16px; }
.onboarding-error { border: 1px solid var(--warning); border-radius: var(--radius); }
.onboarding-error button { min-height: 44px; margin-left: 12px; padding: 8px 14px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: transparent; color: var(--fg); font: inherit; cursor: pointer; }
</style>
