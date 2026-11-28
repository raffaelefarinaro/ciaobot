<template>
  <section class="inventory" aria-label="OpenCode configured providers">
    <p class="hint inventory-note">OpenCode lists configured providers without checking their credentials.</p>
    <p v-if="!inventory" class="hint">Provider inventory unavailable on this engine.</p>
    <p v-else-if="inventory.status === 'unknown'" class="hint">OpenCode could not report its configured providers.</p>
    <p v-else-if="inventory.status === 'partial'" class="hint">Some providers may be missing from this inventory.</p>
    <p v-else-if="!inventory.providers.length" class="hint">No configured providers reported.</p>
    <ul v-if="inventory && inventory.status !== 'unknown' && inventory.providers.length" class="inventory-list set-list">
      <CollapsibleRoot
        v-for="provider in inventory.providers"
        :key="provider.id"
        as="li"
        class="set-row"
        :unmount-on-hide="false"
      >
        <div class="set-row-head inventory-row-head">
          <div class="set-row-main">
            <div class="inventory-name">
              <span class="set-row-title">{{ provider.name }}</span>
              <span class="set-tag inventory-id">{{ provider.id }}</span>
            </div>
            <p class="set-row-sub inventory-status">{{ provider.auth_types.map(methodLabel).join(', ') }} · Configured</p>
          </div>
          <CollapsibleTrigger
            v-if="command"
            class="set-link set-link--quiet provider-action"
            :aria-label="`Reconnect ${provider.name} (${provider.id})`"
          >Reconnect</CollapsibleTrigger>
        </div>
        <CollapsibleContent v-if="command">
          <SignInGuidance :workspace="workspace" :command="command" :note="note" :context="`${provider.name} (${provider.id})`" />
        </CollapsibleContent>
      </CollapsibleRoot>
    </ul>
    <CollapsibleRoot v-if="command" class="inventory-connect" :unmount-on-hide="false">
      <CollapsibleTrigger class="btn-small provider-action">
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
          <path d="M8 3v10M3 8h10" />
        </svg>
        Connect provider
      </CollapsibleTrigger>
      <CollapsibleContent>
        <SignInGuidance :workspace="workspace" :command="command" :note="note" context="a provider" />
      </CollapsibleContent>
    </CollapsibleRoot>
  </section>
</template>

<script setup lang="ts">
import { CollapsibleContent, CollapsibleRoot, CollapsibleTrigger } from 'reka-ui'
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

<style scoped src="./settingsPanels.css"></style>
<style scoped>
.inventory { min-width: 0; padding-block: var(--space-2); }
.inventory-note {
  max-width: 70ch;
  color: var(--fg3);
  margin-bottom: var(--space-3);
}
.inventory-list { list-style: none; padding: 0; margin: var(--space-2) 0 0; }
.inventory-row-head { justify-content: space-between; padding-block: var(--space-3); }
.inventory-name {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-2);
}
.inventory-name > * { overflow-wrap: anywhere; white-space: normal; }
.inventory-id {
  flex: 0 1 auto;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 400;
  color: var(--fg3);
}
.inventory-status {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin-top: var(--space-1);
  font-size: var(--text-sm);
}
.inventory-status::before {
  content: '';
  flex: 0 0 0.45em;
  height: 0.45em;
  border-radius: 50%;
  background: currentColor;
}
.inventory-connect { margin-top: var(--space-3); }
@container (max-width: 480px) {
  .inventory-row-head {
    flex-direction: column;
    align-items: flex-start;
    gap: 0;
    padding-block: var(--space-2);
  }
}
</style>
