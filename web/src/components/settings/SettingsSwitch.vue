<template>
  <button
    type="button"
    role="switch"
    class="settings-switch"
    :aria-checked="checked"
    :aria-busy="busy ? 'true' : undefined"
    :disabled="disabled"
    @click="toggle"
  >
    <span class="switch-word">{{ busy ? '…' : checked ? 'On' : 'Off' }}</span>
    <span class="switch-track" aria-hidden="true"></span>
  </button>
</template>

<script setup lang="ts">
const props = withDefaults(defineProps<{
  checked: boolean
  disabled?: boolean
  busy?: boolean
}>(), { disabled: false, busy: false })
const emit = defineEmits<{ toggle: [] }>()
function toggle() {
  if (!props.disabled) emit('toggle')
}
</script>

<style scoped>
/* A switch reads as state, keeping pink for the accent rather than an action.
   The visible On/Off word means it does not rely on colour. */
.settings-switch {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  padding: 0 2px;
  border: 0;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.settings-switch:disabled { opacity: 0.6; cursor: default; }
.settings-switch:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
.settings-switch .switch-word { min-width: 2.2em; text-align: right; }
.settings-switch .switch-track {
  position: relative;
  width: 36px;
  height: 20px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-full, 9999px);
  background: var(--bg3);
  transition: background 120ms ease, border-color 120ms ease;
}
.settings-switch .switch-track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--fg3);
  transition: transform 120ms ease, background 120ms ease;
}
.settings-switch[aria-checked='true'] .switch-word { color: var(--fg); }
.settings-switch[aria-checked='true'] .switch-track {
  border-color: transparent;
  background: var(--accent);
}
.settings-switch[aria-checked='true'] .switch-track::after {
  transform: translateX(16px);
  background: var(--on-accent);
}
@media (prefers-reduced-motion: reduce) {
  .settings-switch .switch-track,
  .settings-switch .switch-track::after { transition: none; }
}
</style>
