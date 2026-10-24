<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import type { MemoryHandover } from '../../lib/types'
import type { OnboardingStageEvents, OnboardingStageProps, StageState } from '../../lib/onboarding'
import { useOnboardingPreferences } from '../../composables/useOnboardingPreferences'

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<OnboardingStageEvents>()
const prefs = useOnboardingPreferences(props.context.target, props.context.workspace)
const canContinue = computed(() => !prefs.pending.value && !prefs.error.value && prefs.draft.acknowledged)
function report() {
  const state: StageState = { dirty: prefs.dirty.value, pending: prefs.pending.value, canContinue: canContinue.value, error: prefs.error.value }
  emit('state', state)
}
watch([() => prefs.pending.value, () => prefs.error.value, () => prefs.dirty.value, () => prefs.draft.acknowledged], report, { immediate: true })
onMounted(() => { void prefs.load() })
async function continueStage() {
  if (!canContinue.value) return
  if (!await prefs.saveMemoryHandover()) { report(); return }
  emit('continue')
}
function select(value: MemoryHandover) { prefs.draft.memoryHandover = value }
</script>

<template>
  <section aria-labelledby="memory-stage-title">
    <h2 id="memory-stage-title">Memory handover</h2>
    <p>Choose whether Workspace care may act on memory suggestions. Accept-and-dismiss files accepted facts into the archive and lets some accepted text enter always-loaded memory; anything uncertain is queued for review instead. History shows what changed and offers Undo, and you can change this later in Settings.</p>
    <p v-if="!prefs.actingAvailable.value && prefs.draft.memoryHandover !== 'off'" role="status" class="hint">
      The trusted service is unavailable, so acceptance is not acting now. Enforcement is unchanged; this choice does not make the service available.
    </p>
    <fieldset :disabled="prefs.pending.value">
      <legend>Memory handover choice</legend>
      <label><input type="radio" name="memory-handover" value="off" :checked="prefs.draft.memoryHandover === 'off'" @change="select('off')"> Off — predict only</label>
      <label><input type="radio" name="memory-handover" value="dismiss" :checked="prefs.draft.memoryHandover === 'dismiss'" @change="select('dismiss')"> Dismiss — dismiss suggestions predicted to be dismissed</label>
      <label><input type="radio" name="memory-handover" value="all" :checked="prefs.draft.memoryHandover === 'all'" @change="select('all')"> Accept and dismiss — files accepted facts and lets accepted text into always-loaded memory, with uncertain facts queued for review</label>
    </fieldset>
    <label class="ack"><input v-model="prefs.draft.acknowledged" type="checkbox"> I understand this choice, including that accepted facts may be filed, that some text may enter always-loaded memory, and that anything uncertain is queued for review.</label>
    <p v-if="prefs.error.value" role="alert">{{ prefs.error.value }}</p>
    <p v-if="prefs.draft.memoryHandover === prefs.savedHandover.value" class="hint">Saved choice: {{ prefs.savedHandover.value }}.</p>
    <div class="stage-actions"><button type="button" class="onboarding-button back" :disabled="props.busy || prefs.pending.value" @click="emit('back')">Back</button><button type="button" class="onboarding-button onboarding-primary" :disabled="props.busy || !canContinue" @click="continueStage">Continue</button><button type="button" class="onboarding-button" @click="emit('refresh')">Refresh</button></div>
  </section>
</template>

<style scoped>
h2 { margin: 0 0 12px; font-size: 18px; }
p { line-height: 1.55; }
fieldset { display: grid; gap: 8px; border: 0; padding: 0; margin: 20px 0; }
legend { margin-bottom: 8px; font-weight: 600; }
fieldset label, .ack { display: flex; align-items: flex-start; gap: 10px; min-height: 44px; line-height: 1.45; }
input { accent-color: var(--accent); width: 18px; height: 18px; flex: 0 0 auto; }
.stage-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 20px; }
.onboarding-button { min-height: 44px; padding: 8px 14px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: transparent; color: var(--fg); font: inherit; cursor: pointer; }
.onboarding-primary { margin-left: auto; color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.onboarding-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.onboarding-button:disabled { opacity: .55; cursor: not-allowed; }
.hint { color: var(--fg2); }
</style>
