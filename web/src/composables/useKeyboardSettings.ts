import { ref } from 'vue'
import { api } from '../lib/api'
import type { KeyboardSettings } from '../lib/keyboardShortcuts'

const settings = ref<KeyboardSettings>({ keyboard_shortcuts: {}, keyboard_send_mode: 'modifier' })
const loaded = ref(false)
let loading: Promise<void> | null = null

export function applyKeyboardSettings(value: Partial<KeyboardSettings>): void {
  if (value.keyboard_shortcuts && typeof value.keyboard_shortcuts === 'object') {
    settings.value.keyboard_shortcuts = { ...value.keyboard_shortcuts }
  }
  if (value.keyboard_send_mode === 'modifier' || value.keyboard_send_mode === 'enter') {
    settings.value.keyboard_send_mode = value.keyboard_send_mode
  }
  loaded.value = true
}

async function load(): Promise<void> {
  if (loading) return loading
  loading = api.get<KeyboardSettings>('/api/settings/keyboard')
    .then(applyKeyboardSettings)
    .finally(() => { loading = null })
  return loading
}

export function useKeyboardSettings(): {
  settings: typeof settings
  loaded: typeof loaded
  load: () => Promise<void>
  save: (patch: Partial<KeyboardSettings>) => Promise<void>
} {
  return {
    settings,
    loaded,
    load,
    async save(patch) {
      const value = await api.patch<KeyboardSettings>('/api/settings/keyboard', patch)
      applyKeyboardSettings(value)
    },
  }
}
