<template>
  <section class="card" id="mcp-servers">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <h2 class="section-title">MCP servers</h2>
        <p class="hint">Connections belong to Claude Code or OpenCode. This list shows their configuration for the <strong>{{ status?.workspace || 'selected' }}</strong> workspace and connection status from open chats.</p>
      </div>
      <button class="btn-small" type="button" :disabled="loading" @click="fetchStatus">{{ loading ? 'Checking…' : 'Refresh' }}</button>
    </div>
    <p v-if="error" class="action-result --error" role="alert">{{ error }}</p>
    <p v-if="loading" class="hint" role="status">Reading CLI configuration and open chat connections…</p>
    <template v-if="status">
      <p class="hint">Run setup commands on the computer running Ciaobot, in this workspace folder:</p>
      <code class="mcp-workspace-path">{{ status.agent_root }}</code>
      <p class="hint">Add and authenticate a server separately in each CLI you use. After changing configuration, start a new chat to load it.</p>
      <section v-for="inventory in status.providers" :key="inventory.provider" class="mcp-provider" :aria-labelledby="`mcp-${inventory.provider}-title`">
        <div class="mcp-provider-heading">
          <h3 :id="`mcp-${inventory.provider}-title`">{{ providerLabel(inventory.provider) }}</h3>
          <span v-if="!inventory.available" class="badge badge--muted">not installed</span>
        </div>
        <p v-if="inventory.inventory_state === 'unknown'" class="hint" role="status">Some CLI configuration could not be read. Check this workspace in {{ providerLabel(inventory.provider) }} for the complete list.</p>
        <p v-else-if="!inventory.servers.length" class="hint">No MCP servers configured for this CLI in this workspace.</p>
        <div v-for="server in inventory.servers" :key="server.name" class="mcp-server">
          <div class="mcp-server-heading">
            <span class="skill-name">{{ server.name }}</span>
            <span class="badge badge--muted">{{ scopeLabel(server.scope) }}</span>
            <span v-if="server.allowed === false" class="badge badge--warn">blocked in workspace</span>
          </div>
          <p v-if="server.allowed === false" class="hint">This workspace’s tool restrictions prevent access to this server.</p>
          <div v-for="connection in server.connections" :key="connection.chat_id" class="mcp-connection">
            <span class="badge" :class="stateClass(connection.state)">{{ stateLabel(connection.state) }}</span>
            <a :href="`/chat/${encodeURIComponent(connection.chat_id)}`" :aria-label="`Open ${providerLabel(inventory.provider)} chat ${connection.chat_id}`">Open chat</a>
            <p v-if="connection.error" class="hint">{{ connection.error }}</p>
          </div>
          <p v-if="!server.connections.length" class="hint">Connection not checked. Start a {{ providerLabel(inventory.provider) }} chat in this workspace to check it.</p>
        </div>
        <details class="mcp-setup">
          <summary>Set up MCP in {{ providerLabel(inventory.provider) }}</summary>
          <p class="hint">Replace the example name and URL with your server’s details. These commands add it only for this workspace.</p>
          <pre tabindex="0" role="region" :aria-label="`${providerLabel(inventory.provider)} workspace setup commands`"><code>{{ inventory.setup.add }}</code></pre>
          <p class="hint">For a server that requires OAuth sign-in:</p>
          <pre tabindex="0" role="region" :aria-label="`${providerLabel(inventory.provider)} authentication commands`"><code>{{ inventory.setup.authenticate }}</code></pre>
          <p class="hint">Sign-in runs through the CLI on the host. For a remote host, connect over SSH and follow the CLI’s browser or headless sign-in instructions.</p>
          <a class="mcp-doc-link" :href="inventory.provider === 'claude' ? 'https://code.claude.com/docs/en/mcp' : 'https://opencode.ai/v2/docs/mcp-servers'" target="_blank" rel="noopener noreferrer">{{ providerLabel(inventory.provider) }} MCP instructions</a>
        </details>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import type { McpServersController } from '../../composables/useMcpServers'
import type { McpConnection, McpProviderInventory } from '../../lib/types'

const props = defineProps<{ mcp: McpServersController }>()
const { status, loading, error, fetchStatus } = props.mcp

function providerLabel(provider: McpProviderInventory['provider']): string {
  return provider === 'claude' ? 'Claude Code' : 'OpenCode'
}

function scopeLabel(scope: string): string {
  const labels: Record<string, string> = { local: 'this workspace', project: 'project', user: 'all workspaces', global: 'all workspaces', account: 'account', provider: 'provider' }
  return labels[scope] || scope
}

function stateLabel(state: McpConnection['state']): string {
  return { connected: 'connected', failed: 'connection failed', disabled: 'disabled', needs_auth: 'sign-in required', unknown: 'connection not checked' }[state]
}

function stateClass(state: McpConnection['state']): string {
  return state === 'connected' ? 'badge--success' : state === 'failed' ? 'badge--error' : 'badge--warn'
}
</script>

<style scoped src="./settingsPanels.css"></style>
<style scoped>
.mcp-workspace-path { display: block; overflow-wrap: anywhere; font-size: var(--text-sm); }
.mcp-provider { margin-top: var(--space-5); }
.mcp-provider-heading, .mcp-server-heading { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.mcp-provider-heading h3 { margin: 0; font-size: var(--text-base); font-weight: 600; }
.mcp-server { padding: var(--space-3) 0; border-bottom: 1px solid var(--border); }
.mcp-server-heading { margin-bottom: var(--space-2); }
.mcp-server .hint { margin: var(--space-2) 0 0; }
.mcp-connection { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin-top: var(--space-2); }
.mcp-connection .hint { flex-basis: 100%; }
.mcp-connection a, .mcp-doc-link { color: var(--accent); font-size: var(--text-sm); }
.mcp-setup { margin-top: var(--space-3); }
.mcp-setup summary { cursor: pointer; color: var(--fg2); font-size: var(--text-sm); padding: var(--space-2) 0; }
.mcp-setup pre { max-width: 100%; overflow-x: auto; background: var(--bg); padding: var(--space-3); border-radius: var(--radius-sm); font-size: var(--text-sm); }
@media (pointer: coarse) {
  .mcp-setup summary, .mcp-connection a, .mcp-doc-link { min-height: var(--touch); display: inline-flex; align-items: center; }
}
</style>
