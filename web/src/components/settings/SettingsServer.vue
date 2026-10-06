<template>
  <!-- Server settings that used to be lines in the workspace .env. The bind
       address and the log level are read when the engine starts, so a saved
       change says so until the next restart. -->
  <div class="card">
    <div class="settings-card-header">
      <h2 class="section-title">Network access</h2>
      <p class="hint">
        Where this engine listens. Other devices need it to listen on every network
        interface; the password protects it either way. Takes effect after a restart.
      </p>
    </div>
    <label class="settings-field">
      <span class="ws-label">Listen on</span>
      <select
        class="routine-select"
        :value="hostChoice"
        :disabled="!routines || routinesSaving"
        @change="saveHost(($event.target as HTMLSelectElement).value)"
      >
        <option value="">Every network interface (LAN, Tailscale)</option>
        <option value="127.0.0.1">This computer only</option>
        <option v-if="customHost" :value="customHost">Custom address: {{ customHost }}</option>
      </select>
    </label>
    <p v-if="hostPending" class="hint hint--warn" role="status">
      Saved. Restart Ciaobot to listen on {{ effectiveHost }}.
    </p>
  </div>

  <div class="card">
    <div class="settings-card-header">
      <h2 class="section-title">Developer</h2>
      <p class="hint">
        For working on Ciaobot itself. Developer mode shows the Debug card and lets Restart
        deploy from a source checkout.
      </p>
    </div>
    <div class="dev-control">
      <span class="hint">Developer mode</span>
      <button
        class="dev-toggle"
        type="button"
        role="switch"
        :aria-checked="devMode"
        aria-label="Developer mode"
        :disabled="!routines || routinesSaving"
        @click="saveRoutines({ dev_mode: !devMode })"
      >
        <span class="switch-word">{{ devMode ? 'On' : 'Off' }}</span>
        <span class="switch-track" aria-hidden="true"></span>
      </button>
    </div>
    <div class="checkout-row">
      <label class="settings-field checkout-field">
        <span class="ws-label">Source checkout</span>
        <input
          v-model="appRepoDraft"
          type="text"
          class="routine-input"
          placeholder="/absolute/path/to/ciaobot"
          spellcheck="false"
          autocomplete="off"
          :disabled="!routines || routinesSaving"
          @keydown.enter.prevent="saveAppRepo"
        />
      </label>
      <button
        class="btn-secondary btn-small"
        type="button"
        :disabled="!routines || routinesSaving || appRepoDraft.trim() === (routines?.app_repo || '')"
        @click="saveAppRepo"
      >Save</button>
    </div>
    <label class="settings-field">
      <span class="ws-label">Log level</span>
      <select
        class="routine-select"
        :value="logLevelChoice"
        :disabled="!routines || routinesSaving"
        @change="saveRoutines({ log_level: ($event.target as HTMLSelectElement).value })"
      >
        <option v-for="level in logLevels" :key="level" :value="level === defaultLogLevel ? '' : level">
          {{ levelLabel(level) }}
        </option>
      </select>
    </label>
    <p v-if="logLevelPending" class="hint hint--warn" role="status">
      Saved. Restart Ciaobot to log at {{ effectiveLogLevel }}.
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { RoutineSettings } from '../../lib/types'

// The settings store is owned by SettingsView (it also feeds the Models tab).
const props = defineProps<{
  routines: RoutineSettings | null
  routinesSaving: boolean
  saveRoutines: (patch: Record<string, unknown>) => Promise<void>
}>()

const LOOPBACK = '127.0.0.1'

const defaultHost = computed(() => props.routines?.server_defaults?.pwa_host || '0.0.0.0')
const defaultLogLevel = computed(() => props.routines?.server_defaults?.log_level || 'info')
const logLevels = computed(
  () => props.routines?.server_defaults?.log_levels || ['debug', 'info', 'warning', 'error'],
)

const storedHost = computed(() => props.routines?.pwa_host || '')
const customHost = computed(() =>
  storedHost.value && storedHost.value !== LOOPBACK && storedHost.value !== defaultHost.value
    ? storedHost.value
    : '',
)
const hostChoice = computed(() =>
  storedHost.value === defaultHost.value ? '' : storedHost.value,
)
const effectiveHost = computed(() => storedHost.value || defaultHost.value)
const hostPending = computed(
  () => !!props.routines?.server_running && props.routines.server_running.pwa_host !== effectiveHost.value,
)

const logLevelChoice = computed(() => {
  const stored = props.routines?.log_level || ''
  return stored === defaultLogLevel.value ? '' : stored
})
const effectiveLogLevel = computed(() => props.routines?.log_level || defaultLogLevel.value)
const logLevelPending = computed(
  () => !!props.routines?.server_running && props.routines.server_running.log_level !== effectiveLogLevel.value,
)

const devMode = computed(() => props.routines?.dev_mode === true)

const appRepoDraft = ref('')
watch(
  () => props.routines?.app_repo,
  (value) => {
    appRepoDraft.value = value || ''
  },
  { immediate: true },
)

function levelLabel(level: string): string {
  const name = level.charAt(0).toUpperCase() + level.slice(1)
  if (level === defaultLogLevel.value) return `${name} (default)`
  if (level === 'debug') return `${name} — also writes .runtime/server_debug.log`
  return name
}

function saveHost(value: string) {
  void props.saveRoutines({ pwa_host: value })
}

function saveAppRepo() {
  void props.saveRoutines({ app_repo: appRepoDraft.value.trim() })
}
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
/* The checkout path and its Save button read as one control. */
.checkout-row {
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
}
.checkout-field {
  flex: 1 1 auto;
  min-width: 0;
}
.checkout-row .btn-small {
  flex: 0 0 auto;
  min-height: var(--touch);
}

/* One hairline row: the label, a switch on the right (SettingsInsights). */
.dev-control {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  min-height: 56px;
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.dev-control .hint {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
}
.dev-toggle {
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
.dev-toggle:disabled { opacity: 0.6; cursor: default; }
.dev-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
.switch-word { min-width: 2.2em; text-align: right; }
.switch-track {
  position: relative;
  width: 36px;
  height: 20px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-full, 9999px);
  background: var(--bg3);
  transition: background 120ms ease, border-color 120ms ease;
}
.switch-track::after {
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
.dev-toggle[aria-checked='true'] .switch-word { color: var(--fg); }
.dev-toggle[aria-checked='true'] .switch-track {
  border-color: transparent;
  background: var(--accent);
}
.dev-toggle[aria-checked='true'] .switch-track::after {
  transform: translateX(16px);
  background: var(--on-accent);
}
@media (prefers-reduced-motion: reduce) {
  .switch-track, .switch-track::after { transition: none; }
}
</style>
