<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../lib/api'
import type { ProviderConfigSettings, ProviderConnection } from '../lib/types'

// Shown under a chat turn that failed because the provider CLI is signed out.
// Ciaobot keeps retrying on its own; this tells the user how to sign back in
// so the next try can succeed. The command comes from the engine's provider
// status, because it names the exact binary Ciaobot runs (the one inside the
// app when nothing is on PATH), which a hardcoded `claude auth login` would
// not reach.
const props = defineProps<{
  provider: string
  actionLabel: string
  actionDisabled?: boolean
}>()
const emit = defineEmits<{ (e: 'action'): void }>()

const connection = ref<ProviderConnection | null>(null)
const loadError = ref('')
const loading = ref(false)
const copied = ref(false)

async function onToggle(event: Event) {
  if (!(event.target as HTMLDetailsElement).open || connection.value || loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    const res = await api.get<ProviderConfigSettings>('/api/settings/providers')
    const row = res.connections?.[props.provider]
    if (row?.command) connection.value = row
    else loadError.value = 'Could not find the sign-in command for this provider.'
  } catch (e) {
    loadError.value = `Could not look up the sign-in command: ${(e as Error)?.message || e}`
  } finally {
    loading.value = false
  }
}

async function copy() {
  const cmd = connection.value?.command
  if (!cmd || !navigator.clipboard) return
  try {
    await navigator.clipboard.writeText(cmd)
    // Only claim a copy that happened; the command stays selectable text.
    copied.value = true
  } catch {
    // Clipboard access can be denied (no permission, no secure context).
  }
}
</script>

<template>
  <details class="signin-hint" @toggle="onToggle">
    <summary class="signin-hint-summary">How to sign in again</summary>
    <div class="signin-hint-body">
      <p v-if="loading" class="signin-hint-status">Looking up the sign-in command…</p>
      <p v-else-if="loadError" class="signin-hint-status" role="alert">{{ loadError }} Settings → Models has the provider's sign-in button.</p>
      <ol v-else-if="connection" class="signin-hint-steps">
        <li>On the computer running Ciaobot, open a terminal (Terminal on a Mac, PowerShell on Windows).</li>
        <li>
          Paste this command, press Enter and finish signing in{{ connection.label ? ` to ${connection.label}` : '' }} in the browser window it opens:
          <div class="signin-hint-cmd">
            <code>{{ connection.command }}</code>
            <button class="btn-small" type="button" @click="copy">{{ copied ? 'Copied' : 'Copy' }}</button>
          </div>
        </li>
        <li>
          Come back here and click <strong>{{ actionLabel }}</strong>.
          <button
            class="btn-small signin-hint-action"
            type="button"
            :disabled="actionDisabled"
            @click="emit('action')"
          >{{ actionLabel }}</button>
        </li>
      </ol>
    </div>
  </details>
</template>

<style scoped>
.signin-hint {
  margin-top: var(--space-2);
  font-size: var(--text-sm);
}
.signin-hint-summary {
  cursor: pointer;
  color: var(--accent);
  font-weight: 600;
  min-height: 32px;
  width: fit-content;
  padding: 4px 0;
}
.signin-hint-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: 4px;
}
.signin-hint-body { margin-top: var(--space-2); }
.signin-hint-status { color: var(--fg2); margin: 0; }
.signin-hint-steps {
  margin: 0;
  padding-left: 1.4em;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  line-height: 1.45;
}
.signin-hint-cmd {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: 6px;
}
.signin-hint-cmd code {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
  user-select: all;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--bg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}
.signin-hint-action { margin-left: var(--space-2); }

@media (pointer: coarse) {
  .signin-hint-summary { min-height: 44px; padding: 12px 0; }
  .signin-hint .btn-small { min-height: 44px; }
}
</style>
