<template>
  <section class="card" aria-labelledby="keyboard-shortcuts-title">
    <div class="settings-card-header">
      <p id="keyboard-shortcuts-title" class="section-title">Keyboard shortcuts</p>
      <p class="hint">Shortcuts are shared by every device connected to this Ciaobot engine.</p>
    </div>
    <div class="shortcut-setting-list">
      <div v-for="id in shortcutIds" :key="id" class="shortcut-setting-row">
        <span class="shortcut-setting-name">{{ SHORTCUT_LABELS[id] }}</span>
        <kbd>{{ captureId === id ? 'Press a shortcut…' : displayBinding(effectiveBinding(settings, id), apple) }}</kbd>
        <button class="btn-small" type="button" @click="captureId = captureId === id ? null : id">
          {{ captureId === id ? 'Cancel' : 'Change' }}
        </button>
        <button class="btn-small" type="button" @click="toggleBinding(id)">{{ effectiveBinding(settings, id) === 'disabled' ? 'Enable' : 'Turn off' }}</button>
        <button class="btn-small" type="button" :disabled="effectiveBinding(settings, id) === DEFAULT_SHORTCUTS[id]" @click="resetBinding(id)">Reset</button>
      </div>
    </div>
    <label class="shortcut-send-setting">
      <span>
        <span class="shortcut-setting-name">Send a message with</span>
        <span class="hint">On touch keyboards, Enter always inserts a newline.</span>
      </span>
      <select :value="settings.keyboard_send_mode" :disabled="pending" @change="setSendMode">
        <option value="modifier">Cmd/Ctrl+Enter</option>
        <option value="enter">Enter (Shift+Enter for a newline)</option>
      </select>
    </label>
    <p class="hint">Comment and file edit fields continue to save with Cmd/Ctrl+Enter.</p>
    <button class="settings-disclosure" type="button" :disabled="pending" @click="restoreDefaults">Restore all defaults</button>
    <p v-if="captureError || error" class="action-result" role="alert">{{ captureError || error }}</p>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useKeyboardSettings } from '../../composables/useKeyboardSettings'
import { bindingFromEvent, DEFAULT_SHORTCUTS, displayBinding, effectiveBinding, SHORTCUT_LABELS, type KeyboardSettings, type ShortcutId } from '../../lib/keyboardShortcuts'
import { isApplePlatform } from '../../lib/platform'

const shortcutIds = Object.keys(DEFAULT_SHORTCUTS) as ShortcutId[]
const { settings, load, save } = useKeyboardSettings()
const apple = isApplePlatform()
const pending = ref(false)
const error = ref('')
const captureError = ref('')
const captureId = ref<ShortcutId | null>(null)

onMounted(() => { void load().catch(() => { error.value = 'Could not load keyboard settings.' }) })
onMounted(() => window.addEventListener('keydown', onCaptureKeydown, true))
onBeforeUnmount(() => window.removeEventListener('keydown', onCaptureKeydown, true))

function onCaptureKeydown(event: KeyboardEvent): void {
  if (!captureId.value) return
  event.preventDefault()
  event.stopImmediatePropagation()
  if (event.key === 'Escape' && !(event.metaKey || event.ctrlKey || event.altKey || event.shiftKey)) {
    captureId.value = null
    captureError.value = ''
    return
  }
  const binding = bindingFromEvent(event, captureId.value.startsWith('workspace'))
  if (!binding) return
  const conflict = shortcutIds.find(id => id !== captureId.value && effectiveBinding(settings.value, id) === binding)
  if (conflict) {
    captureError.value = `That shortcut is already assigned to ${SHORTCUT_LABELS[conflict]}. Choose another key.`
    return
  }
  void setBinding(captureId.value, binding)
  captureId.value = null
  captureError.value = ''
}

async function update(patch: Partial<KeyboardSettings>): Promise<void> {
  pending.value = true
  error.value = ''
  try { await save(patch) } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not save keyboard settings.'
  } finally { pending.value = false }
}

function setBinding(id: ShortcutId, binding: string): Promise<void> {
  return update({ keyboard_shortcuts: { ...settings.value.keyboard_shortcuts, [id]: binding } })
}

function resetBinding(id: ShortcutId): Promise<void> {
  const next = { ...settings.value.keyboard_shortcuts }
  delete next[id]
  return update({ keyboard_shortcuts: next })
}

function toggleBinding(id: ShortcutId): Promise<void> {
  return setBinding(id, effectiveBinding(settings.value, id) === 'disabled' ? DEFAULT_SHORTCUTS[id] : 'disabled')
}

function setSendMode(event: Event): void {
  const mode = (event.target as HTMLSelectElement).value
  if (mode === 'modifier' || mode === 'enter') void update({ keyboard_send_mode: mode })
}

function restoreDefaults(): Promise<void> {
  return update({ keyboard_shortcuts: {}, keyboard_send_mode: 'modifier' })
}
</script>

<style scoped>
.shortcut-setting-list { display: grid; gap: 8px; }
.shortcut-setting-row { display: grid; grid-template-columns: minmax(180px, 1fr) auto auto auto auto; align-items: center; gap: 8px; min-height: 44px; }
.shortcut-setting-row kbd { min-width: 94px; text-align: center; }
.shortcut-setting-name { font-size: var(--text-sm); }
.shortcut-send-setting { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 0; }
.shortcut-send-setting > span { display: grid; gap: 4px; }
.shortcut-send-setting select { min-height: 44px; max-width: 100%; }
.shortcut-setting-row button { min-height: 44px; }
@media (max-width: 680px) {
  .shortcut-setting-row { grid-template-columns: 1fr auto auto; padding: 8px 0; border-bottom: 1px solid var(--border); }
  .shortcut-setting-name { grid-column: 1 / -1; }
  .shortcut-setting-row kbd { justify-self: start; }
  .shortcut-send-setting { align-items: stretch; flex-direction: column; }
}
</style>
