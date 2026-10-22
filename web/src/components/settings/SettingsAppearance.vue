<template>
  <div class="setting-row setting-row--inline">
    <div class="routine-info">
      <span class="routine-name">Text contrast</span>
      <span class="routine-detail">Soft makes body text a little dimmer so it glows less on dark backgrounds.</span>
    </div>
    <div class="settings-control">
      <div class="instance-toggle" role="radiogroup" aria-label="Text contrast">
        <button
          v-for="option in TEXT_CONTRAST_OPTIONS"
          :key="option.value"
          class="toggle-btn"
          type="button"
          role="radio"
          :aria-checked="settings.text_contrast === option.value"
          :class="{ active: settings.text_contrast === option.value }"
          :disabled="pending"
          @click="choose(option.value)"
        >
          {{ option.label }}
        </button>
      </div>
    </div>
  </div>
  <p v-if="error" class="action-result" role="alert">{{ error }}</p>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useAppearanceSettings } from '../../composables/useAppearanceSettings'
import type { TextContrast } from '../../lib/appearance'

const TEXT_CONTRAST_OPTIONS: { value: TextContrast; label: string }[] = [
  { value: 'normal', label: 'Normal' },
  { value: 'soft', label: 'Soft' },
]

const { settings, load, save } = useAppearanceSettings()
const pending = ref(false)
const error = ref('')

onMounted(() => {
  void load().catch(() => { error.value = 'Could not load appearance settings.' })
})

async function choose(value: TextContrast) {
  if (pending.value || settings.value.text_contrast === value) return
  pending.value = true
  error.value = ''
  try {
    await save({ text_contrast: value })
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not save appearance settings.'
  } finally {
    pending.value = false
  }
}
</script>

<style scoped src="./settingsPanels.css"></style>
