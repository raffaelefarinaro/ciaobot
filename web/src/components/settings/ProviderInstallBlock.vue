<template>
  <p v-if="missing" class="set-row-sub">
    <template v-if="connKey !== 'opencode'">{{ conn.detail }}</template>
    Install it with <code>{{ conn.command }}</code>
    <template v-if="conn.path_command">
      , then put it on your PATH with
      <code>{{ conn.path_command }}</code>
    </template>&nbsp;<a
      v-if="conn.install_url"
      :href="conn.install_url"
      target="_blank"
      rel="noopener noreferrer"
    >installation guide ↗</a>
  </p>
  <p v-if="(missing || conn.auth === 'unsupported_version') && conn.terminal_action" class="set-row-sub">
    <HostTerminalButton
      :action="conn.terminal_action"
      safe-errors
      variant="small"
      :aria-label="`Install ${conn.label || connKey} in Terminal`"
      label="Install in Terminal"
    />
  </p>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import HostTerminalButton from '../HostTerminalButton.vue'
import { isMissingInstall } from '../../lib/providerState'
import type { ProviderConnection } from '../../lib/types'

// Shown only when the CLI or its adapter is missing, and only ever a user-initiated
// Terminal action: nothing here installs a CLI by itself.
const props = defineProps<{ connKey: string; conn: ProviderConnection }>()

const missing = computed(() => isMissingInstall(props.conn.state))
</script>
