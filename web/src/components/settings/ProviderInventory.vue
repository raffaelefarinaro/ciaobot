<template>
  <section class="inventory" aria-label="OpenCode configured providers">
    <p class="hint">OpenCode reports configured providers; their credentials have not been verified.</p>
    <p v-if="!inventory" class="hint">Provider inventory unavailable on this engine.</p>
    <p v-else-if="inventory.status === 'unknown'" class="hint">OpenCode could not report its configured providers.</p>
    <p v-else-if="inventory.status === 'partial'" class="hint">Some providers may be missing from this inventory.</p>
    <p v-else-if="!inventory.providers.length" class="hint">No configured providers reported.</p>
    <ul v-if="inventory && inventory.status !== 'unknown' && inventory.providers.length" class="inventory-list">
      <li v-for="provider in inventory.providers" :key="provider.id" class="inventory-row">
        <div><strong>{{ provider.name }}</strong> <span class="hint">({{ provider.id }})</span></div>
        <p class="hint">{{ provider.auth_types.map(methodLabel).join(', ') }} · Configured · verification unknown</p>
        <details>
          <summary>Reconnect {{ provider.name }} ({{ provider.id }})</summary>
          <SignInGuidance :workspace="workspace" cli="opencode" :context="`${provider.name} (${provider.id})`" />
        </details>
      </li>
    </ul>
    <details>
      <summary>Connect provider</summary>
      <SignInGuidance :workspace="workspace" cli="opencode" context="a provider" />
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

defineProps<{ inventory?: ProviderInventoryData, workspace: string }>()

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
