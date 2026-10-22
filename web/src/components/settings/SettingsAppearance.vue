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
  <div class="setting-row setting-row--inline">
    <div class="routine-info">
      <span class="routine-name">Custom colours</span>
      <span class="routine-detail">{{ customColorsDetail }}</span>
    </div>
    <div class="settings-control custom-colors-actions">
      <button class="btn-small" type="button" @click="customOpen = true">Customise…</button>
      <button
        v-if="hasCustomColors"
        class="btn-small"
        type="button"
        :disabled="resetting"
        @click="resetCustomColors"
      >
        Reset
      </button>
    </div>
  </div>
  <p v-if="softIgnored" class="hint">
    Soft text contrast has no effect while custom colours are on for this theme.
  </p>
  <CustomColorsDialog v-model:open="customOpen" />
  <p v-if="error" class="action-result" role="alert">{{ error }}</p>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useAppearanceSettings } from '../../composables/useAppearanceSettings'
import CustomColorsDialog from './CustomColorsDialog.vue'
import SettingsRadioGroup from './SettingsRadioGroup.vue'
import { isLightTheme } from '../../lib/theme'
import { checkCustomPair } from '../../lib/contrast'
import type { AppearancePayload, BodyFont, TextContrast } from '../../lib/appearance'

const TEXT_CONTRAST_OPTIONS: { value: TextContrast; label: string }[] = [
  { value: 'normal', label: 'Normal' },
  { value: 'soft', label: 'Soft' },
]
// Each option is drawn in its own face so the choice is visible before it is made.
const BODY_FONT_OPTIONS: { value: BodyFont; label: string; fontFamily: string }[] = [
  { value: 'system', label: 'System', fontFamily: 'var(--font-sans)' },
  { value: 'serif', label: 'Serif', fontFamily: 'var(--font-serif)' },
  { value: 'mono', label: 'Mono', fontFamily: 'var(--font-mono)' },
]

const { settings, load, save } = useAppearanceSettings()
const error = ref('')

const customOpen = ref(false)
const resetting = ref(false)
// Only a pair that passes the readability check is painted, so only that counts.
function paintedPair(mode: 'dark' | 'light'): boolean {
  const pair = settings.value.custom_colors?.[mode]
  return !!pair && checkCustomPair(pair).ok
}
const customModes = computed(() => (['dark', 'light'] as const).filter(paintedPair))
const hasCustomColors = computed(() => customModes.value.length > 0)
const customColorsDetail = computed(() => {
  if (customModes.value.length === 2) return 'Using your own colours for both themes.'
  if (customModes.value.length === 1) return `Using your own colours for the ${customModes.value[0]} theme.`
  return 'Choose your own background and text colours.'
})
const softIgnored = computed(() => settings.value.text_contrast === 'soft'
  && paintedPair(isLightTheme.value ? 'light' : 'dark'))

async function resetCustomColors() {
  resetting.value = true
  error.value = ''
  try {
    await save({ custom_colors: {} })
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not save appearance settings.'
  } finally {
    resetting.value = false
  }
}

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
