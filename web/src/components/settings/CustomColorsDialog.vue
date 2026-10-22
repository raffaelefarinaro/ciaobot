<template>
  <DialogRoot :open="open" modal @update:open="onOpenChange">
    <!-- Portalled: Settings sits inside a container-type pane, which would
         otherwise become the containing block of this fixed overlay. -->
    <DialogPortal>
      <DialogOverlay as-child>
        <div class="confirm-backdrop">
          <DialogContent class="confirm-card colors-card" aria-modal="true">
            <DialogTitle as="p" class="confirm-title">Custom colours</DialogTitle>
            <DialogDescription as="p" class="confirm-message">
              Pick a background and text colour for each theme. Colours that are hard to read are refused.
            </DialogDescription>
            <section v-for="section in sections" :key="section.mode" class="colors-section">
              <h3 class="colors-heading">{{ section.label }}</h3>
              <div v-for="field in FIELDS" :key="field.key" class="colors-row">
                <label class="colors-label" :for="`custom-${section.mode}-${field.key}`">{{ field.label }}</label>
                <input
                  class="colors-swatch"
                  type="color"
                  :aria-label="`${section.label} ${field.label.toLowerCase()} colour`"
                  :value="pickerValue(section.mode, field.key)"
                  @input="setColor(section.mode, field.key, ($event.target as HTMLInputElement).value)"
                >
                <input
                  :id="`custom-${section.mode}-${field.key}`"
                  class="colors-hex"
                  type="text"
                  inputmode="text"
                  spellcheck="false"
                  autocomplete="off"
                  :aria-label="`${section.label} ${field.label.toLowerCase()} hex`"
                  :value="draft[section.mode][field.key]"
                  @input="setColor(section.mode, field.key, ($event.target as HTMLInputElement).value)"
                >
              </div>
              <div class="colors-preview" :style="previewStyle(section.mode)" aria-hidden="true">
                The quick brown fox
              </div>
              <p
                class="colors-status"
                :class="{ 'hint--warn': !section.result.ok }"
                role="status"
              >
                <template v-if="section.result.ok">{{ section.result.ratio.toFixed(1) }}:1 Readable</template>
                <template v-else>{{ section.result.reason }}</template>
              </p>
            </section>
            <p v-if="error" class="action-result --error" role="alert">{{ error }}</p>
            <div class="confirm-actions">
              <button
                class="confirm-action confirm-action--cancel colors-reset"
                type="button"
                :disabled="saving"
                @click="reset"
              >
                Reset
              </button>
              <DialogClose as-child>
                <button class="confirm-action confirm-action--cancel" type="button">Cancel</button>
              </DialogClose>
              <button
                class="confirm-action confirm-action--primary"
                type="button"
                :disabled="!canApply"
                @click="apply"
              >
                Apply
              </button>
            </div>
          </DialogContent>
        </div>
      </DialogOverlay>
    </DialogPortal>
  </DialogRoot>
</template>

<script setup lang="ts">
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'
import { computed, reactive, ref, watch } from 'vue'
import { useAppearanceSettings } from '../../composables/useAppearanceSettings'
import { DEFAULT_PAIRS } from '../../lib/appearance'
import { checkCustomPair, parseHex, toHex, type ColorPair, type PairCheck, type ThemeMode } from '../../lib/contrast'
import { isLightTheme } from '../../lib/theme'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [open: boolean] }>()

const FIELDS: { key: keyof ColorPair; label: string }[] = [
  { key: 'bg', label: 'Background' },
  { key: 'fg', label: 'Text' },
]
const MODES: { mode: ThemeMode; label: string }[] = [
  { mode: 'dark', label: 'Dark theme' },
  { mode: 'light', label: 'Light theme' },
]

const { settings, save } = useAppearanceSettings()
const draft = reactive<Record<ThemeMode, ColorPair>>({
  dark: { ...DEFAULT_PAIRS.dark },
  light: { ...DEFAULT_PAIRS.light },
})
// What each section started from; a section equal to its seed is untouched.
const seed = reactive<Record<ThemeMode, ColorPair>>({
  dark: { ...DEFAULT_PAIRS.dark },
  light: { ...DEFAULT_PAIRS.light },
})
const error = ref('')
const saving = ref(false)

function themeColor(name: '--bg' | '--fg', fallback: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  const rgb = parseHex(raw)
  return rgb ? toHex(rgb) : fallback
}

/** The saved pair, else the colours the theme shows now (or its defaults when not showing). */
function seedFor(mode: ThemeMode): ColorPair {
  const saved = settings.value.custom_colors?.[mode]
  if (saved) return { ...saved }
  const active: ThemeMode = isLightTheme.value ? 'light' : 'dark'
  if (mode !== active) return { ...DEFAULT_PAIRS[mode] }
  return {
    bg: themeColor('--bg', DEFAULT_PAIRS[mode].bg),
    // Soft contrast dims the computed --fg; a custom pair starts from the theme's own text colour.
    fg: settings.value.text_contrast === 'soft'
      ? DEFAULT_PAIRS[mode].fg
      : themeColor('--fg', DEFAULT_PAIRS[mode].fg),
  }
}

watch(() => props.open, (open) => {
  if (!open) return
  error.value = ''
  for (const { mode } of MODES) {
    seed[mode] = seedFor(mode)
    draft[mode] = { ...seed[mode] }
  }
}, { immediate: true })

const sections = computed(() => MODES.map(({ mode, label }) => ({
  mode,
  label,
  result: checkCustomPair(draft[mode]) as PairCheck,
})))

function edited(mode: ThemeMode): boolean {
  return draft[mode].bg !== seed[mode].bg || draft[mode].fg !== seed[mode].fg
}

const canApply = computed(() => {
  const touched = MODES.filter(({ mode }) => edited(mode))
  return !saving.value && touched.length > 0
    && touched.every(({ mode }) => checkCustomPair(draft[mode]).ok)
})

function setColor(mode: ThemeMode, key: keyof ColorPair, value: string): void {
  const rgb = parseHex(value)
  // A picker value is already #rrggbb; typed text is kept as typed until valid.
  draft[mode][key] = rgb && /^#[0-9a-f]{6}$/i.test(value.trim()) ? toHex(rgb) : value.trim()
}

function pickerValue(mode: ThemeMode, key: keyof ColorPair): string {
  const rgb = parseHex(draft[mode][key])
  return rgb ? toHex(rgb) : seed[mode][key]
}

function previewStyle(mode: ThemeMode): Record<string, string> {
  const bg = parseHex(draft[mode].bg)
  const fg = parseHex(draft[mode].fg)
  return {
    '--preview-bg': bg ? toHex(bg) : seed[mode].bg,
    '--preview-fg': fg ? toHex(fg) : seed[mode].fg,
  }
}

function onOpenChange(next: boolean): void {
  if (!next && saving.value) return
  emit('update:open', next)
}

async function persist(custom_colors: NonNullable<typeof settings.value.custom_colors>): Promise<void> {
  saving.value = true
  error.value = ''
  try {
    await save({ custom_colors })
    emit('update:open', false)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not save custom colours.'
  } finally {
    saving.value = false
  }
}

function apply(): void {
  if (!canApply.value) return
  const next = { ...settings.value.custom_colors }
  for (const { mode } of MODES) {
    if (!edited(mode)) continue
    // The normalised pair: a typed #fff is stored as #ffffff, which the engine requires.
    const result = checkCustomPair(draft[mode])
    if (result.ok) next[mode] = { bg: result.tokens['--bg'], fg: result.tokens['--fg'] }
  }
  void persist(next)
}

function reset(): void {
  void persist({})
}
</script>

<style scoped>
.confirm-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgb(0 0 0 / 55%);
}
.confirm-card {
  width: 100%;
  max-width: 30rem;
  max-height: 90dvh;
  overflow: auto;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--fg);
  box-shadow: 0 1.5rem 3rem rgb(0 0 0 / 45%);
}
.confirm-title {
  margin: 0 0 var(--space-2);
  font-weight: 600;
}
.confirm-message {
  margin: 0 0 var(--space-4);
  color: var(--fg2);
}
.colors-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
.colors-heading {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: 600;
}
.colors-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.colors-label {
  flex: 0 0 6.5rem;
  color: var(--fg2);
  font-size: var(--text-sm);
}
.colors-swatch {
  flex: 0 0 auto;
  width: 2.75rem;
  height: 2.75rem;
  padding: 2px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg);
  cursor: pointer;
}
.colors-hex {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 2.75rem;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}
.colors-preview {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--preview-bg);
  color: var(--preview-fg);
}
.colors-status {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}
.confirm-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-2);
}
.colors-reset {
  margin-right: auto;
}
.confirm-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 6rem;
  min-height: 2.75rem;
  padding: 0 var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
  transition:
    background 120ms var(--ease),
    border-color 120ms var(--ease),
    color 120ms var(--ease),
    transform 120ms var(--ease);
}
.confirm-action:active:not(:disabled) {
  transform: scale(0.98);
}
.confirm-action:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.confirm-action--cancel {
  border-color: var(--border-strong);
  background: var(--bg3);
}
.confirm-action--cancel:hover:not(:disabled) {
  background: var(--border-strong);
}
.confirm-action--primary {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--on-accent);
}
.confirm-action--primary:hover:not(:disabled) {
  border-color: var(--accent-strong);
  background: var(--accent-strong);
}
</style>
