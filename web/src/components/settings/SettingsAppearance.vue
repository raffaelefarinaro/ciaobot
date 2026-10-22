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
        :model-value="textContrast.desired.value ?? settings.text_contrast"
        @update:model-value="textContrast.choose"
      />
    </div>
  </div>
  <div class="setting-row setting-row--inline">
    <div class="routine-info">
      <span class="routine-name">Body font</span>
      <span class="routine-detail">Applies to chat text and menus. Code stays monospace.</span>
    </div>
    <div class="settings-control">
      <SettingsRadioGroup
        label="Body font"
        :options="BODY_FONT_OPTIONS"
        :model-value="bodyFont.desired.value ?? settings.body_font"
        @update:model-value="bodyFont.choose"
      />
    </div>
  </div>
  <p v-if="error" class="action-result" role="alert">{{ error }}</p>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useAppearanceSettings } from '../../composables/useAppearanceSettings'
import SettingsRadioGroup from './SettingsRadioGroup.vue'
import type { AppearancePayload, BodyFont, TextContrast } from '../../lib/appearance'

const TEXT_CONTRAST_OPTIONS: { value: TextContrast; label: string }[] = [
  { value: 'normal', label: 'Normal' },
  { value: 'soft', label: 'Soft' },
]
// Each option is drawn in its own face so the choice is visible before it is made.
const face = (name: 'sans' | 'serif' | 'mono') => `var(--font-${name})`
const BODY_FONT_OPTIONS: { value: BodyFont; label: string; fontFamily: string }[] = [
  { value: 'system', label: 'System', fontFamily: face('sans') },
  { value: 'serif', label: 'Serif', fontFamily: face('serif') },
  { value: 'mono', label: 'Mono', fontFamily: face('mono') },
]

const { settings, load, save } = useAppearanceSettings()
const error = ref('')

onMounted(() => {
  void load().catch(() => { error.value = 'Could not load appearance settings.' })
})

/**
 * One choice of the page. The latest pick is shown at once and saved when no
 * save of this control is in flight, so the user's last choice wins over one
 * made while an earlier save was pending.
 */
function useChoice<K extends 'text_contrast' | 'body_font'>(key: K) {
  const pending = ref(false)
  const desired = ref<AppearancePayload[K] | null>(null)

  async function choose(value: AppearancePayload[K]) {
    desired.value = value
    if (pending.value) return
    pending.value = true
    error.value = ''
    try {
      while (desired.value !== null && desired.value !== settings.value[key]) {
        await save({ [key]: desired.value } as Partial<AppearancePayload>)
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : 'Could not save appearance settings.'
    } finally {
      desired.value = null
      pending.value = false
    }
  }
  return { desired, choose }
}

const textContrast = useChoice('text_contrast')
const bodyFont = useChoice('body_font')
</script>

<style scoped src="./settingsPanels.css"></style>
