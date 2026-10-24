<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useTaskStore } from '../../stores/tasks'
import type { OnboardingStageProps, OnboardingStageEvents, StageState } from '../../lib/onboarding'
import type { Schedule } from '../../lib/types'

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<OnboardingStageEvents>()
const store = useTaskStore()
const routine = ref<Schedule | null>(null)
const time = ref('08:00')
const timezone = ref(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
const availability = ref('always on')
const keepDefault = ref(false)
const savedOnce = ref(false)
const saving = ref(false)
const error = ref('')
const touchedTime = ref(false)
// What the engine currently has; `dirty` compares the proposal against it.
const saved = ref({ time: '08:00', zone: 'UTC' })
const dirty = computed(() =>
  !!routine.value && (time.value !== saved.value.time || timezone.value !== saved.value.zone),
)
// Continue needs a decision that still matches the fields: a later edit or a
// failed save invalidates it, and a proposal is not a saved choice.
const state = computed<StageState>(() => ({
  dirty: dirty.value,
  pending: saving.value,
  canContinue: !!routine.value && !saving.value && !error.value && (keepDefault.value || (savedOnce.value && !dirty.value)),
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
async function save() {
  if (!routine.value || !dirty.value) return
  // Snapshot what is being sent: edits made while the request is in flight must
  // stay dirty rather than being reported as saved.
  const submitted = { time: time.value, zone: timezone.value }
  saving.value = true
  error.value = ''
  try {
    await store.updateSchedule(routine.value.schedule_id, { time: submitted.time, timezone: submitted.zone })
    saved.value = submitted
    keepDefault.value = false
    savedOnce.value = true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not save care timing.'
    savedOnce.value = false
  } finally { saving.value = false }
}
// Any edit abandons an earlier "keep the packaged default": the fields no
// longer describe that decision.
watch([time, timezone], () => { keepDefault.value = false })

function onAvailability(): void {
  if (!touchedTime.value) time.value = suggestedTime()
}
function useDefault() { keepDefault.value = true; savedOnce.value = false }
onMounted(() => { void load() })
defineExpose({ refresh: load })
</script>

<template>
  <section class="care-stage" aria-labelledby="care-title">
    <h2 id="care-title">When should workspace care run?</h2>
    <p>Care runs while this computer is awake. The first run may catch up on the latest missed slot at startup. You can edit or pause it later in Automations.</p>
    <p v-if="routine">Workspace care is {{ routine.enabled ? 'on' : 'paused' }}.</p>
    <p v-if="!routine && !error" role="status">No care routine is installed for this workspace. Nothing was created.</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="routine">
      <fieldset><legend>When is the host usually available?</legend>
        <label v-for="answer in ['always on', 'daytime', 'varies']" :key="answer">
          <input
            v-model="availability"
            type="radio"
            name="availability"
            :value="answer"
            @change="onAvailability"
          > {{ answer }}
        </label>
      </fieldset>
      <p>The suggested time is a local slot; availability only guides this suggestion.</p>
      <div class="fields">
        <label>Local time <input v-model="time" type="time" required @input="touchedTime = true" :aria-invalid="!!error" :aria-describedby="error ? 'care-error' : undefined"></label>
        <label>Timezone <input v-model="timezone" name="timezone" autocomplete="off" required></label>
      </div>
      <p v-if="error" id="care-error" class="error">{{ error }}</p>
      <div class="actions">
        <button type="button" :disabled="!dirty || saving || props.busy" aria-label="Save care timing" @click="save">{{ saving ? 'Saving…' : 'Save timing' }}</button>
        <button type="button" :disabled="saving || props.busy" aria-label="Keep packaged default" @click="useDefault">Keep packaged default</button>
      </div>
      <p v-if="keepDefault" role="status">Keeping the packaged default. Nothing was written.</p>
      <p v-else-if="!dirty" role="status">Timing is saved.</p>
    </template>
  </section>
</template>

<style scoped>
.care-stage { display: grid; gap: 12px; }
h2 { margin: 0; font: 700 18px/1.35 var(--font); }
p { margin: 0; color: var(--fg2); line-height: 1.55; }
fieldset { display: flex; flex-wrap: wrap; gap: 8px 16px; border: 0; padding: 0; margin: 0; }
legend { margin-bottom: 8px; font-weight: 600; }
fieldset label { min-height: 44px; display: inline-flex; align-items: center; gap: 8px; text-transform: capitalize; }
.fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.fields label { display: grid; gap: 6px; }
input:not([type="radio"]) { width: 100%; min-height: 44px; padding: 8px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: var(--bg); color: var(--fg); font: inherit; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
button { min-height: 44px; padding: 8px 14px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: transparent; color: var(--fg); font: inherit; cursor: pointer; }
button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button:disabled { opacity: .55; cursor: not-allowed; }
.error { color: var(--error); }
</style>
