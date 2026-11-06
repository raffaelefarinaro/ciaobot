<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import type { MemoryHandover } from '../../lib/types'
import type { OnboardingStageProps, StageState } from '../../lib/onboarding'
import { useOnboardingPreferences } from '../../composables/useOnboardingPreferences'
import OnboardingStack from './OnboardingStack.vue'
import OnboardingChoiceGroup from './OnboardingChoiceGroup.vue'
import OnboardingChoice from './OnboardingChoice.vue'

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState] }>()
const prefs = useOnboardingPreferences(props.context.target, props.context.workspace)
const canContinue = computed(() => prefs.loaded.value && !prefs.pending.value && !prefs.error.value)
function report() {
  emit('state', { dirty: prefs.dirty.value, pending: prefs.pending.value, canContinue: canContinue.value, error: prefs.error.value })
}
watch([() => prefs.pending.value, () => prefs.error.value, () => prefs.dirty.value, () => prefs.loaded.value], report, { immediate: true })
onMounted(() => { void prefs.load() })

// A choice is written when it is made. The proposed default is the only value
// that can still be unsaved; the shell commits it through `save` on Continue.
async function select(value: string): Promise<void> {
  if (prefs.pending.value) return
  prefs.draft.memoryHandover = value as MemoryHandover
  await commit()
}
// A failed save puts the group back on what is stored, so tapping the option
// again (including the suggested one) is a change and retries.
async function commit(): Promise<boolean> {
  if (await prefs.saveMemoryHandover()) return true
  prefs.draft.memoryHandover = prefs.savedHandover.value
  return false
}
const save = commit
defineExpose({ save })
</script>

<template>
  <OnboardingStack as="section" gap="xl" aria-labelledby="memory-stage-title">
    <OnboardingStack>
      <h2 id="memory-stage-title">Memory handover</h2>
      <p class="hint">Choose how Workspace care handles suggestions about your notes. Anything it files or skips shows up in History, where you can Undo it, and you can change this later in Settings.</p>
    </OnboardingStack>
    <p v-if="!prefs.actingAvailable.value && prefs.draft.memoryHandover !== 'off'" role="status" class="hint">
      The trusted service is unavailable, so acceptance is not acting now. Enforcement is unchanged; this choice does not make the service available.
    </p>
    <OnboardingChoiceGroup label="When Ciaobot suggests a memory" name="memory-handover" :model-value="prefs.draft.memoryHandover" @update:model-value="select">
      <OnboardingChoice value="off">
        Ask me first
        <template #description>Nothing is filed or skipped for you; every suggestion waits for review.</template>
      </OnboardingChoice>
      <OnboardingChoice value="dismiss">
        Skip likely duplicates
        <template #description>Suggestions that look like repeats are dismissed; the rest wait for review.</template>
      </OnboardingChoice>
      <OnboardingChoice value="all">
        File what's clear, ask about the rest
        <template #description>Clear facts are filed to the archive and some text may enter always-loaded memory; anything uncertain is queued for review.</template>
      </OnboardingChoice>
      <template #after>
        <p v-if="prefs.dirty.value && !prefs.pending.value" id="memory-suggested" class="hint">Suggested. Saved when you continue.</p>
      </template>
    </OnboardingChoiceGroup>
    <p v-if="prefs.error.value" class="error" role="alert">{{ prefs.error.value }}</p>
  </OnboardingStack>
</template>

<style scoped>
.hint { color: var(--fg2); line-height: 1.5; }
.error { margin: 0; color: var(--error); }
</style>
