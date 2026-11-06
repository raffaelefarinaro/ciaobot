<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useTaskStore } from '../../stores/tasks'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import OnboardingStack from './OnboardingStack.vue'
import OnboardingField from './OnboardingField.vue'
import OnboardingChoiceGroup from './OnboardingChoiceGroup.vue'
import OnboardingChoice from './OnboardingChoice.vue'
import type { Schedule } from '../../lib/types'

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState] }>()
const store = useTaskStore()
const routine = ref<Schedule | null>(null)
const time = ref('08:00')
const timezone = ref(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
const availability = ref('always on')
const saving = ref(false)
const error = ref('')
const touchedTime = ref(false)
// Only an edit by the person is ever written; the browser-local proposal that
// load() puts in the fields is not a choice.
const edited = ref(false)
// What the engine currently has; `dirty` compares the proposal against it.
const saved = ref({ time: '08:00', zone: 'UTC' })
const dirty = computed(() =>
  !!routine.value && (time.value !== saved.value.time || timezone.value !== saved.value.zone),
)
const pendingWrite = computed(() => edited.value && dirty.value)
// Continue is never a dead end: with nothing edited it writes nothing, and an
// edit is committed by `save`, so only a pending write or an error blocks it.
const state = computed<StageState>(() => ({
  dirty: pendingWrite.value,
  pending: saving.value,
  canContinue: !saving.value && !error.value,
  error: error.value || null,
}))
watch([state, () => props.busy], () => emit('state', state.value), { immediate: true, deep: true })

async function load() {
  error.value = ''
  try {
    await store.fetchSchedules()
    routine.value = store.schedules.find(row => row.scope === 'system' && row.schedule_id === `system-memory-curation@${props.context.workspace}`) || null
    if (routine.value) {
      saved.value = { time: routine.value.daily_time_utc, zone: routine.value.timezone_name }
      // The suggestion is local to this browser; the routine's packaged zone is
      // only the baseline the proposal is compared against.
      time.value = suggestedTime()
      timezone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || saved.value.zone
    }
  } catch (cause) { error.value = cause instanceof Error ? cause.message : 'Could not load care timing.' }
}
// Availability guides the proposal only; the operator can always override it.
function suggestedTime(): string {
  if (availability.value === 'daytime') return '13:00'
  if (availability.value === 'varies') return '09:00'
  return '08:00'
}
// Called by the shell on Continue. Unchanged fields write nothing.
async function save(): Promise<boolean> {
  if (saving.value) return false
  if (!routine.value || !pendingWrite.value) return true
  // Snapshot what is being sent: edits made while the request is in flight must
  // stay dirty rather than being reported as saved.
  const submitted = { time: time.value, zone: timezone.value }
  saving.value = true
  error.value = ''
  try {
    await store.updateSchedule(routine.value.schedule_id, { time: submitted.time, timezone: submitted.zone })
    saved.value = submitted
    return true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not save care timing.'
    return false
  } finally { saving.value = false }
}

function chooseAvailability(value: string): void {
  availability.value = value
  edited.value = true
  if (!touchedTime.value) time.value = suggestedTime()
}
onMounted(() => { void load() })
defineExpose({ refresh: load, save })
</script>

<template>
  <OnboardingStack as="section" gap="xl" class="care-stage" aria-labelledby="care-title">
    <OnboardingStack>
      <h2 id="care-title">When should workspace care run?</h2>
      <p class="hint">Care runs while this computer is awake. The first run may catch up on the latest missed slot at startup. You can edit or pause it later in Automations.</p>
      <p v-if="routine" class="hint">Workspace care is {{ routine.enabled ? 'on' : 'paused' }}.</p>
      <p v-if="!routine && !error" role="status" class="hint">No care routine is installed for this workspace. Nothing was created.</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </OnboardingStack>
    <template v-if="routine">
      <OnboardingChoiceGroup label="When is the host usually available?" name="availability" layout="segmented" :model-value="availability" @update:model-value="chooseAvailability">
        <OnboardingChoice v-for="answer in ['always on', 'daytime', 'varies']" :key="answer" :value="answer" class="availability">{{ answer }}</OnboardingChoice>
        <template #after><p class="hint">The suggested time is a local slot; availability only guides this suggestion.</p></template>
      </OnboardingChoiceGroup>
      <div class="fields">
        <OnboardingField id="care-time" label="Local time">
          <template #default="{ describedby }">
            <input id="care-time" v-model="time" type="time" required :aria-invalid="!!error" :aria-describedby="describedby" @input="touchedTime = true; edited = true">
          </template>
        </OnboardingField>
        <OnboardingField id="care-timezone" label="Timezone">
          <template #default="{ describedby }">
            <input id="care-timezone" v-model="timezone" name="timezone" autocomplete="off" required :aria-describedby="describedby" @input="edited = true">
          </template>
        </OnboardingField>
      </div>
      <p class="hint" role="status">{{ pendingWrite ? 'Saved when you continue.' : 'The packaged default stays unless you change it. Nothing is written.' }}</p>
    </template>
  </OnboardingStack>
</template>

<style scoped>
.hint { color: var(--fg2); line-height: 1.5; }
.fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr)); gap: var(--space-4, 16px); }
.availability { text-transform: capitalize; }
.error { margin: 0; color: var(--error); }
</style>
