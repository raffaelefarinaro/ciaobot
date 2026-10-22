<template>
  <div class="setting-row setting-row--inline">
    <div class="routine-info">
      <span class="routine-name">Text contrast</span>
      <span class="routine-detail">Soft makes body text a little dimmer so it glows less on dark backgrounds.</span>
    </div>
    <div class="settings-control">
      <SettingsRadioGroup
        label="Text contrast"
        :options="TEXT_CONTRAST_OPTIONS"
        :model-value="desired ?? settings.text_contrast"
        @update:model-value="choose"
      />
    </div>
  </div>
  <p v-if="error" class="action-result" role="alert">{{ error }}</p>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useAppearanceSettings } from '../../composables/useAppearanceSettings'
import SettingsRadioGroup from './SettingsRadioGroup.vue'
import type { TextContrast } from '../../lib/appearance'

const TEXT_CONTRAST_OPTIONS: { value: TextContrast; label: string }[] = [
  { value: 'normal', label: 'Normal' },
  { value: 'soft', label: 'Soft' },
]

const { settings, load, save } = useAppearanceSettings()
const pending = ref(false)
const error = ref('')
// The latest choice, shown at once; it is saved when no save is in flight, so
// the user's last choice wins over one made while an earlier save was pending.
const desired = ref<TextContrast | null>(null)

onMounted(() => {
  void load().catch(() => { error.value = 'Could not load appearance settings.' })
})

async function choose(value: TextContrast) {
  desired.value = value
  if (pending.value) return
  pending.value = true
  error.value = ''
  try {
    while (desired.value !== null && desired.value !== settings.value.text_contrast) {
      await save({ text_contrast: desired.value })
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not save appearance settings.'
  } finally {
    desired.value = null
    pending.value = false
  }
}
</script>

<style scoped src="./settingsPanels.css"></style>
