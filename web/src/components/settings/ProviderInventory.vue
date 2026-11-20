<template>
  <section class="inventory" aria-label="OpenCode configured providers">
    <p class="hint">OpenCode reports which providers are configured. It does not check that their credentials work.</p>
    <p v-if="!inventory" class="hint">Provider inventory unavailable on this engine.</p>
    <p v-else-if="inventory.status === 'unknown'" class="hint">OpenCode could not report its configured providers.</p>
    <p v-else-if="inventory.status === 'partial'" class="hint">Some providers may be missing from this inventory.</p>
    <p v-else-if="!inventory.providers.length" class="hint">No configured providers reported.</p>
    <ul v-if="inventory && inventory.status !== 'unknown' && inventory.providers.length" class="inventory-list">
      <li v-for="provider in inventory.providers" :key="provider.id" class="inventory-row">
        <div><strong>{{ provider.name }}</strong> <span class="hint">({{ provider.id }})</span></div>
        <p class="hint">{{ provider.auth_types.map(methodLabel).join(', ') }} · Configured</p>
        <details v-if="command">
          <summary>Reconnect {{ provider.name }} ({{ provider.id }})</summary>
          <SignInGuidance :workspace="workspace" :command="command" :note="note" :context="`${provider.name} (${provider.id})`" />
        </details>
      </li>
    </ul>
    <details v-if="command">
      <summary>Connect provider</summary>
      <SignInGuidance :workspace="workspace" :command="command" :note="note" context="a provider" />
    </details>
  </section>
</template>

<script setup lang="ts">
import SignInGuidance from './ProviderSignInGuidance.vue'

export interface ProviderInventoryData {
  status: 'complete' | 'partial' | 'unknown'
  providers: {
    id: string
    name: string
    auth_types: ('oauth' | 'api_key' | 'environment' | 'unknown')[]
    configured: true
    verified: null
  }[]
}

// The OpenCode row's own sign-in command, from the connection payload. Absent
// when no OpenCode row was reported, so no sign-in guidance is offered.
defineProps<{ inventory?: ProviderInventoryData, workspace: string, command?: string }>()

const note = "Run this command yourself on the engine's computer and choose the provider in OpenCode. The command does not preselect one. OpenCode must be installed and available on your PATH."

function methodLabel(method: string): string {
  return ({ oauth: 'OAuth', api_key: 'API key', environment: 'Environment', unknown: 'Unknown' } as Record<string, string>)[method] || 'Unknown'
}
</script>

<style scoped>
.inventory { min-width: 0; }
.inventory-list { list-style: none; padding: 0; margin: 0; }
.inventory-row { padding: var(--space-3) 0; border-bottom: 1px solid var(--border); overflow-wrap: anywhere; }
summary { cursor: pointer; }
@media (pointer: coarse) { summary { min-height: 44px; display: flex; align-items: center; } }
</style>
