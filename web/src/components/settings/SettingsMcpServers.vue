<template>
  <div class="card" id="mcp-servers">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <p class="section-title">mcp servers</p>
        <p class="hint">
          Model Context Protocol (MCP) servers and tools available to Ciaobot agents.
        </p>
      </div>
      <div class="settings-card-header-actions">
        <button class="btn-small" @click="emit('create-via-chat')">Add via chat</button>
        <button class="btn-small" @click="toggleAddServer">
          {{ showAddServer ? 'Cancel' : '+ New MCP server' }}
        </button>
      </div>
    </div>

    <!-- Add MCP Server Form -->
    <div v-if="showAddServer" class="settings-form-panel">
      <div class="settings-field-grid">
        <label class="settings-field">
          <span class="ws-label">Server Name</span>
          <input class="routine-input" v-model="newName" :disabled="addingServer" placeholder="e.g. postgres-db" />
        </label>
        <label class="settings-field">
          <span class="ws-label">Transport Type</span>
          <select class="routine-select" v-model="newTransport" :disabled="addingServer">
            <option value="http">HTTP / SSE</option>
            <option value="stdio">stdio (Command)</option>
          </select>
        </label>
        <label v-if="newTransport === 'http'" class="settings-field settings-field--wide">
          <span class="ws-label">Server URL</span>
          <input class="routine-input" v-model="newUrl" :disabled="addingServer" placeholder="https://mcp.example.com/http" />
        </label>
        <label v-else class="settings-field settings-field--wide">
          <span class="ws-label">Command Line</span>
          <input class="routine-input" v-model="newCommand" :disabled="addingServer" placeholder="npx -y @modelcontextprotocol/server-postgres postgresql://..." />
        </label>
      </div>
      <div class="action-row settings-actions">
        <button class="btn-primary" @click="addCustomServer" :disabled="addingServer || !newName.trim() || (newTransport === 'http' ? !newUrl.trim() : !newCommand.trim())">
          {{ addingServer ? 'Adding...' : 'Add MCP server' }}
        </button>
      </div>
      <div v-if="addServerResult" class="action-result" :class="{ '--error': addServerError }">{{ addServerResult }}</div>
    </div>

    <!-- List of MCP Servers (exact skill-list / skill-row UI) -->
    <div class="skill-list">
      <!-- 1. Built-in Ciaobot FastMCP Server -->
      <div
        class="skill-row"
        :class="{ expanded: isExpanded('ciaobot-fastmcp') }"
        @click="toggleServer('ciaobot-fastmcp')"
      >
        <div class="skill-main">
          <div class="skill-title-row command-title-row">
            <span class="skill-chevron">{{ isExpanded('ciaobot-fastmcp') ? '&#9662;' : '&#9656;' }}</span>
            <span class="skill-name">ciaobot</span>
            <span class="skill-badges">
              <span :class="assetOriginClass('builtin')">{{ assetOriginLabel('builtin') }}</span>
              <span class="badge" :class="fastMcpEnabled ? 'badge--success' : 'badge--muted'">
                {{ fastMcpEnabled ? 'enabled' : 'disabled' }}
              </span>
            </span>
          </div>
          <p class="skill-description">Vault, chats, projects, and schedules.</p>
          <div v-if="isExpanded('ciaobot-fastmcp')" class="skill-detail" @click.stop>
            <p class="skill-meta"><span class="skill-meta-label">Endpoint</span><code>http://127.0.0.1:8443/mcp/</code></p>
            <div class="setting-row setting-row--inline setting-row--toggle" style="margin-top: 8px;">
              <span class="routine-name">FastMCP Control Plane Active</span>
              <label class="settings-checkbox-hit">
                <input type="checkbox" class="settings-checkbox" v-model="fastMcpEnabled" @change="saveFastMcpToggle" />
              </label>
            </div>
            <p class="skill-meta" style="margin-top: 8px;"><span class="skill-meta-label">Embedded Tools ({{ embeddedTools.length }})</span></p>
            <div class="mcp-tag-grid mcp-tag-grid--wide">
              <span v-for="tool in embeddedTools" :key="tool" class="mcp-tag mcp-tag--embedded">{{ tool }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 2. Custom & Project .mcp.json Servers -->
      <template v-if="status?.project_servers && status.project_servers.length">
        <div
          v-for="srv in status.project_servers"
          :key="srv.name"
          class="skill-row"
          :class="{ expanded: isExpanded(srv.name) }"
          @click="toggleServer(srv.name)"
        >
          <div class="skill-main">
            <div class="skill-title-row command-title-row">
              <span class="skill-chevron">{{ isExpanded(srv.name) ? '&#9662;' : '&#9656;' }}</span>
              <span class="skill-name">{{ srv.name }}</span>
              <span class="skill-badges">
                <span :class="assetOriginClass(mcpServerOrigin(srv))">{{ assetOriginLabel(mcpServerOrigin(srv)) }}</span>
                <span
                  class="badge"
                  :class="srv.ready === false ? 'badge--warn' : 'badge--success'"
                >
                  {{ srv.ready === false ? 'needs .env' : 'ready' }}
                </span>
              </span>
            </div>
            <p v-if="srv.url" class="skill-description">URL: {{ srv.url }}</p>
            <p v-else-if="srv.command" class="skill-description">
              Command: {{ srv.command }}<template v-if="srv.args?.length"> {{ srv.args.join(' ') }}</template>
            </p>
            <div v-if="isExpanded(srv.name)" class="skill-detail" @click.stop>
              <div class="settings-field-grid mcp-edit-grid">
                <label v-if="(editDraft(srv).transport || srv.transport) === 'http'" class="settings-field settings-field--wide">
                  <span class="ws-label">URL</span>
                  <input
                    class="routine-input"
                    :value="editDraft(srv).url"
                    :disabled="serverSaving === srv.name"
                    aria-label="MCP server URL"
                    @input="setEditField(srv.name, 'url', ($event.target as HTMLInputElement).value)"
                  />
                </label>
                <template v-else>
                  <label class="settings-field">
                    <span class="ws-label">Command</span>
                    <input
                      class="routine-input"
                      :value="editDraft(srv).command"
                      :disabled="serverSaving === srv.name"
                      aria-label="MCP server command"
                      @input="setEditField(srv.name, 'command', ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                  <label class="settings-field settings-field--wide">
                    <span class="ws-label">Args</span>
                    <input
                      class="routine-input"
                      :value="editDraft(srv).argsText"
                      :disabled="serverSaving === srv.name"
                      placeholder="e.g. -y @notionhq/notion-mcp-server"
                      aria-label="MCP server args"
                      @input="setEditField(srv.name, 'argsText', ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                </template>
                <p v-if="srv.env_path || status?.env_path" class="settings-field settings-field--wide hint hint--compact">
                  Secrets are saved to <code>{{ srv.env_path || status?.env_path }}</code>. Connection config stays in <code>.mcp.json</code>.
                </p>
              </div>

              <div class="mcp-env-block">
                <p class="skill-meta">
                  <span class="skill-meta-label">Secrets for this server</span>
                </p>
                <p class="hint hint--compact">
                  Paste the token into the field below. It is saved to the workspace <code>.env</code> (not into <code>.mcp.json</code>).
                </p>
                <div
                  v-for="envKey in envKeysFor(srv)"
                  :key="`${srv.name}:${envKey.key}`"
                  class="credential-row mcp-env-row"
                >
                  <div class="setting-row-main setting-row-main--inline">
                    <div class="routine-info">
                      <span class="routine-name">{{ envKey.key }}</span>
                      <p v-if="envKey.hint" class="hint hint--compact">{{ envKey.hint }}</p>
                    </div>
                    <span class="badge" :class="envKey.configured ? 'badge--success' : 'badge--error'">
                      {{ envKey.configured ? 'Configured' : 'Missing' }}
                    </span>
                  </div>
                  <input
                    type="password"
                    class="routine-input"
                    :value="envInputs[envKey.key] || ''"
                    :placeholder="envKey.configured ? '•••••••••••• (leave blank to keep)' : `Paste ${envKey.key}`"
                    :disabled="envSaving"
                    :aria-label="envKey.key"
                    @input="envInputs[envKey.key] = ($event.target as HTMLInputElement).value"
                  />
                </div>
                <p v-if="!envKeysFor(srv).length" class="hint hint--compact">
                  No secrets referenced by this server's <code>.mcp.json</code> config.
                </p>
                <div class="action-row settings-actions">
                  <button
                    class="btn-small"
                    :disabled="envSaving || !hasEnvEdits(srv)"
                    @click="saveEnvKeys(srv)"
                  >
                    {{ envSaving ? 'Saving...' : 'Save secrets' }}
                  </button>
                  <button
                    class="btn-small"
                    :disabled="serverSaving === srv.name || !editDirty(srv)"
                    @click="saveServer(srv)"
                  >
                    {{ serverSaving === srv.name ? 'Saving...' : 'Save connection' }}
                  </button>
                </div>
                <div
                  v-if="(envResult && envResultServer === srv.name) || (serverResult && serverResultName === srv.name)"
                  class="action-result"
                  :class="{ '--error': (envResultServer === srv.name && envError) || (serverResultName === srv.name && serverError) }"
                >{{ (envResultServer === srv.name && envResult) || (serverResultName === srv.name && serverResult) }}</div>
              </div>

              <div class="mcp-tools-block">
                <div class="setting-row setting-row--inline" style="margin-top: 8px;">
                  <p class="skill-meta" style="margin: 0;">
                    <span class="skill-meta-label">
                      Tools ({{ (serverTools[srv.name] || srv.tools || []).length }})
                      <template v-if="srv.tools_source && srv.tools_source !== 'none'">
                        · {{ srv.tools_source }}
                      </template>
                    </span>
                  </p>
                  <button
                    class="btn-small"
                    :disabled="toolsLoading[srv.name]"
                    @click="refreshServerTools(srv)"
                  >
                    {{ toolsLoading[srv.name] ? 'Loading...' : (srv.transport === 'http' ? 'Probe tools' : 'Refresh') }}
                  </button>
                </div>
                <p
                  v-if="toolsError[srv.name] || (!(serverTools[srv.name] || srv.tools || []).length && srv.tools_note)"
                  class="hint hint--compact"
                  :class="{ 'hint--warn': !!toolsError[srv.name] }"
                >
                  {{ toolsError[srv.name] || srv.tools_note }}
                </p>
                <div
                  v-if="(serverTools[srv.name] || srv.tools || []).length"
                  class="mcp-tag-grid mcp-tag-grid--wide"
                >
                  <span
                    v-for="tool in (serverTools[srv.name] || srv.tools || [])"
                    :key="tool"
                    class="mcp-tag mcp-tag--embedded"
                  >{{ tool }}</span>
                </div>
              </div>

              <div class="asset-actions">
                <button class="btn-small btn-danger" @click.stop="deleteCustomServer(srv.name)">Delete</button>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
// Settings → MCP. Markup only: every piece of state and every `/api/mcp/*`
// call belongs to the `useMcpServers` controller SettingsView creates and
// passes in, so this component owns nothing that outlives a render. The one
// action it cannot perform itself — "Add via chat", which creates a chat and
// navigates — is reported as an emit.
//
// The controller object is created once per SettingsView instance and never
// replaced, so destructuring its refs here is safe and lets the template read
// them unwrapped.
import { assetOriginClass, assetOriginLabel, mcpServerOrigin } from '../../lib/assetOrigin'
import type { McpServersController } from '../../composables/useMcpServers'

const props = defineProps<{ mcp: McpServersController }>()

const emit = defineEmits<{ 'create-via-chat': [] }>()

const {
  status,
  embeddedTools,
  fastMcpEnabled,
  showAddServer,
  addingServer,
  addServerResult,
  addServerError,
  newName,
  newTransport,
  newUrl,
  newCommand,
  envInputs,
  envSaving,
  envResult,
  envError,
  envResultServer,
  serverSaving,
  serverResult,
  serverError,
  serverResultName,
  serverTools,
  toolsLoading,
  toolsError,
  toggleAddServer,
  isExpanded,
  toggleServer,
  editDraft,
  setEditField,
  editDirty,
  envKeysFor,
  hasEnvEdits,
  saveEnvKeys,
  saveServer,
  refreshServerTools,
  saveFastMcpToggle,
  addCustomServer,
  deleteCustomServer,
} = props.mcp
</script>

<!-- The rules this markup needs used to live in SettingsView's own scoped
     block, which does not reach a child's subtree. Both sides now load the
     same sheet. -->
<style scoped src="./settingsPanels.css"></style>
