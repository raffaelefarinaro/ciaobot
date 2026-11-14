<template>
  <section class="card" id="mcp-servers">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <h2 class="section-title">MCP servers</h2>
        <p class="hint">Connections belong to Claude Code or OpenCode. This list shows their configuration for the <strong>{{ status?.workspace || 'selected' }}</strong> workspace and connection status from open chats.</p>
      </div>
      <div class="settings-card-header-actions">
        <button v-if="status" class="set-link set-link--quiet mcp-ask-button" type="button" @click="emit('ask-ciao')">Ask Ciao to add one</button>
        <button class="btn-small mcp-refresh" type="button" :disabled="loading || toggling !== null" @click="fetchStatus">{{ loading ? 'Checking…' : 'Refresh' }}</button>
      </div>
    </div>
    <p v-if="error" class="action-result --error" role="alert">{{ error }}</p>
    <p v-if="loading" class="hint" role="status">Reading CLI configuration and open chat connections…</p>
    <template v-if="status">
      <TabBar v-if="providers.length > 1" v-model="selected" :tabs="tabs" label="Command line tool" id-prefix="mcp" />
      <div
        v-if="current"
        class="mcp-provider"
        :role="providers.length > 1 ? 'tabpanel' : undefined"
        :id="providers.length > 1 ? `mcp-panel-${current.provider}` : undefined"
        :aria-labelledby="providers.length > 1 ? `mcp-tab-${current.provider}` : undefined"
      >
        <div v-if="providers.length === 1 || !current.available" class="mcp-provider-heading">
          <h3 v-if="providers.length === 1">{{ providerLabel(current.provider) }}</h3>
          <span v-if="!current.available" class="set-tag">not installed</span>
        </div>
        <p v-if="current.inventory_state === 'unknown'" class="hint" role="status">Some CLI configuration could not be read. Check this workspace in {{ providerLabel(current.provider) }} for the complete list.</p>
        <p v-else-if="!current.servers.length" class="hint">No MCP servers configured for this CLI in this workspace.</p>
        <!-- What every row shares is said once here, not repeated under each server. -->
        <div v-if="current.servers.length" class="mcp-list-notes">
          <p v-if="sharedAppliesTo" class="hint mcp-applies-to">Switches apply to new {{ providerLabel(current.provider) }} chats in <code>{{ sharedAppliesTo }}</code>. Running chats keep their connection until they reload.</p>
          <p v-if="anyUnchecked" class="hint">Connection status comes from open chats. Start a chat with {{ providerLabel(current.provider) }} in this workspace to check the servers marked not checked.</p>
          <p v-if="anyClaudeAi" class="hint">
            claude.ai connectors are connected, reconnected and disconnected on
            <a href="https://claude.ai/settings/connectors" target="_blank" rel="noopener noreferrer">claude.ai, Settings, Connectors</a>.
            To hide every claude.ai connector in this workspace, turn off claude.ai connectors in <router-link to="/settings/workspace">Overview</router-link>.
          </p>
        </div>
        <ul v-if="current.servers.length" class="mcp-list" role="list">
          <li v-for="server in current.servers" :key="server.name" class="mcp-server" :class="{ 'mcp-server--off': !isOn(server) }">
            <div class="mcp-server-row">
              <div class="mcp-server-main">
                <div class="mcp-server-heading">
                  <span class="skill-name">{{ server.name }}</span>
                  <span class="set-tag">{{ scopeLabel(server.scope) }}</span>
                  <span v-if="server.allowed === false" class="badge badge--warn">blocked in workspace</span>
                </div>
                <div v-if="isOn(server) && !server.connections.length" class="mcp-connection">
                  <span class="mcp-unchecked">not checked</span>
                </div>
                <div v-for="group in groupMcpConnections(server.connections)" :key="group.state" class="mcp-connection">
                  <span class="badge" :class="isOn(server) ? stateClass(group.state, server) : 'badge--muted'">{{ stateLabel(group.state, server) }}</span>
                  <span v-if="group.chatIds.length > 1" class="mcp-connection-count">in {{ group.chatIds.length }} chats</span>
                  <a :href="`/chat/${encodeURIComponent(group.chatIds[0]!)}`" :aria-label="`Open ${providerLabel(current.provider)} chat ${group.chatIds[0]}`">Open chat</a>
                  <p v-if="group.error && !claudeAiNotConnected(group.state, server)" class="hint">{{ group.error }}</p>
                </div>
              </div>
              <div class="mcp-server-controls">
                <template v-if="manageable(server) && !isClaudeAi(server)">
                  <button
                    v-if="needsAuth(server)"
                    type="button"
                    class="mcp-signin btn-primary btn-small"
                    :aria-disabled="signingIn !== null ? 'true' : undefined"
                    @click="signInInChat(current.provider, server.name, signInChat(server)!)"
                  >{{ signingIn === moreKey(server) ? 'Starting…' : 'Sign in' }}</button>
                  <button
                    v-else-if="showSignIn(server)"
                    type="button"
                    class="mcp-signin btn-small"
                    @click="runCommand(serverCommand(current.provider, 'login', server.name))"
                  >Sign in in terminal</button>
                  <div class="mcp-more" @keydown.esc.stop="closeMenu(true)" @focusout="onFocusOut">
                    <button
                      :ref="el => setMoreRef(moreKey(server), el)"
                      type="button"
                      class="set-menu-btn mcp-more-button"
                      :aria-label="`More actions for ${server.name}`"
                      aria-haspopup="menu"
                      :aria-expanded="openMenu === moreKey(server)"
                      @click="toggleMenu(moreKey(server))"
                    >···</button>
                    <div v-if="openMenu === moreKey(server)" class="mcp-menu" role="menu" :aria-label="`Actions for ${server.name}`">
                      <button v-if="isOn(server)" type="button" role="menuitem" class="mcp-menuitem" @click="signInFromMenu(current.provider, server.name)">Sign in in terminal</button>
                      <button type="button" role="menuitem" class="mcp-menuitem" @click="copyServerCommand(current.provider, server.name)">{{ current.provider === 'claude' ? 'Copy remove command' : 'Copy sign-out command' }}</button>
                    </div>
                  </div>
                  <span v-if="menuCopied === moreKey(server)" class="hint" role="status">Copied</span>
                </template>
                <button
                  v-if="server.applies_to"
                  type="button"
                  role="switch"
                  class="settings-switch mcp-toggle"
                  :aria-checked="isOn(server)"
                  :aria-label="`${server.name} for ${providerLabel(current.provider)}`"
                  :aria-busy="toggling === moreKey(server) || undefined"
                  @click="toggleServer(current.provider, server)"
                >
                  <span class="switch-word">{{ toggling === moreKey(server) ? '…' : (isOn(server) ? 'On' : 'Off') }}</span>
                  <span class="switch-track" aria-hidden="true"></span>
                </button>
              </div>
            </div>
            <p v-if="server.allowed === false" class="hint">This workspace’s tool restrictions prevent access to this server.</p>
            <p v-if="server.applies_to && !sharedAppliesTo" class="hint mcp-applies-to">Applies to: <code>{{ server.applies_to }}</code></p>
            <p v-if="!server.applies_to && server.scope === 'managed'" class="hint">Managed by your organization; it can’t be turned off here.</p>
            <p v-if="!isClaudeAi(server) && !manageable(server)" class="hint">This server can’t be managed with a command here; use {{ providerLabel(current.provider) }} directly.</p>
            <p v-if="signInError === moreKey(server)" class="action-result --error" role="alert">{{ SIGN_IN_ERROR }}</p>
            <p v-if="needsAuth(server) && !isClaudeAi(server) && manageable(server)" class="hint">Sign-in required. Open the chat's MCP section to finish sign-in.</p>
          </li>
        </ul>
        <p v-if="toggleNote && toggleFor === current.provider" class="hint mcp-toggle-note" role="status">{{ toggleNote }}</p>
        <p v-if="toggleError && toggleFor === current.provider" class="action-result --error" role="alert">{{ toggleError }}</p>

        <details class="mcp-manual">
          <summary>Add a server yourself</summary>
          <template v-if="status.agent_root">
            <p class="hint">Commands run in this workspace folder:</p>
            <code class="mcp-workspace-path">{{ status.agent_root }}</code>
          </template>
          <div class="mcp-fields">
            <div class="mcp-field">
              <label for="mcp-server-name">Server name</label>
              <input id="mcp-server-name" v-model="name" type="text" placeholder="notion" autocomplete="off" autocapitalize="off" spellcheck="false" :aria-invalid="nameInvalid" />
              <p v-if="nameInvalid" class="mcp-error" role="alert">Use letters, numbers, dots, dashes or underscores, starting with a letter or number.</p>
            </div>
            <div class="mcp-field">
              <label for="mcp-server-url">Server URL</label>
              <input id="mcp-server-url" v-model="url" type="text" placeholder="https://example.com/mcp" autocomplete="off" autocapitalize="off" spellcheck="false" :aria-invalid="!!urlError" />
              <p v-if="urlError" class="mcp-error" role="alert">{{ urlError }}</p>
            </div>
          </div>
          <ul class="mcp-commands">
            <li v-for="row in commandRows" :key="row.key" :data-row="row.key">
              <span class="mcp-command-label">{{ row.label }}</span>
              <code>{{ row.display }}</code>
              <span class="mcp-command-buttons">
                <button type="button" class="btn-small" :disabled="!row.command" @click="copyCommand(row.command)">Copy</button>
                <button type="button" class="btn-small" :disabled="!row.command" @click="runCommand(row.command, row.enter === false ? { enter: false } : undefined)">{{ row.enter === false ? 'Type in terminal' : 'Run in terminal' }}</button>
              </span>
            </li>
          </ul>
          <p class="hint" role="status">{{ runNote }}</p>
          <p v-if="current.provider === 'opencode' && current.servers.length" class="hint">OpenCode has no remove command: delete the server from the <code>mcp</code> block of the config file named in its Source.</p>
          <p class="hint">Sign-in runs through the CLI on the host. For a remote host, connect over SSH and follow the CLI’s browser or headless sign-in instructions.</p>
          <a class="mcp-doc-link" :href="current.provider === 'claude' ? 'https://code.claude.com/docs/en/mcp' : 'https://opencode.ai/v2/docs/mcp-servers'" target="_blank" rel="noopener noreferrer">{{ providerLabel(current.provider) }} MCP instructions</a>
        </details>
      </div>
      <p class="hint">Add and authenticate a server separately in each CLI you use. After changing MCP configuration, start a new chat, or run <code>ciao chat reload</code> in an existing one, to load it.</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import TabBar from '../TabBar.vue'
import type { McpServersController } from '../../composables/useMcpServers'
import { useRunInTerminal } from '../../composables/useRunInTerminal'
import { writeClipboard } from '../../lib/codeCopy'
import { errorMessage } from '../../lib/errorMessage'
import { groupMcpConnections } from '../../lib/mcpConnections'
import { mcpCommand, serverUrlError, validServerName, type McpAction } from '../../lib/shellQuote'
import type { McpConnection, McpNativeServer, McpProviderInventory, McpToggleResult } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'

type Provider = McpProviderInventory['provider']

const props = defineProps<{ mcp: McpServersController; defaultProvider?: string; targetProvider?: Provider; targetProviderKey?: number }>()
const emit = defineEmits<{ (e: 'ask-ciao'): void }>()
const { status, loading, error, fetchStatus } = props.mcp
const terminal = useRunInTerminal()
const projectStore = useProjectStore()

const providers = computed<McpProviderInventory[]>(() => {
  const all = status.value?.providers ?? []
  return [...all.filter(p => p.provider === props.defaultProvider), ...all.filter(p => p.provider !== props.defaultProvider)]
})
const tabs = computed(() => providers.value.map(p => ({ key: p.provider, label: providerLabel(p.provider), count: p.servers.length })))
const selected = ref<Provider>('claude')
// Re-pick only when the set of providers or the default changes, so a Refresh
// does not throw the user back to the first tab.
let previousProviders = ''
let previousTargetKey: number | undefined
let previousTargetProvider: Provider | undefined
watch(
  () => [props.defaultProvider, providers.value.map(p => p.provider).join(','), props.targetProvider, props.targetProviderKey] as const,
  ([defaultProvider, available, targetProvider, targetKey]) => {
    if (!available) return
    const signature = `${defaultProvider ?? ''}|${available}`
    if (signature !== previousProviders || targetKey !== previousTargetKey || targetProvider !== previousTargetProvider) {
      selected.value = providers.value.find(p => p.provider === targetProvider)?.provider ?? providers.value[0]!.provider
      previousProviders = signature
      previousTargetKey = targetKey
      previousTargetProvider = targetProvider
    }
  },
  { immediate: true },
)
const current = computed(() => providers.value.find(p => p.provider === selected.value) ?? providers.value[0])

/** The one place every switch on this tab writes to, or null when rows differ (each row then names its own). */
const sharedAppliesTo = computed(() => {
  const targets = new Set((current.value?.servers ?? []).map(sv => sv.applies_to).filter((t): t is string => !!t))
  return targets.size === 1 ? [...targets][0]! : null
})
const anyUnchecked = computed(() => (current.value?.servers ?? []).some(sv => isOn(sv) && !sv.connections.length))
const anyClaudeAi = computed(() => (current.value?.servers ?? []).some(isClaudeAi))

const name = ref('')
const url = ref('')
const nameInvalid = computed(() => name.value !== '' && !validServerName(name.value))
const urlError = computed(() => (url.value !== '' && status.value ? serverUrlError(url.value, status.value.shell.kind) : null))

function command(provider: Provider, action: McpAction, n?: string, u?: string): string | null {
  if (!status.value) return null
  return mcpCommand({ provider, action, name: n, url: u, shell: status.value.shell.kind, cdPrefix: status.value.shell.cd_prefix })
}
function serverCommand(provider: Provider, action: McpAction, serverName: string): string | null {
  return command(provider, action, serverName)
}

interface CommandRow { key: string; label: string; command: string | null; display: string; enter?: boolean }
const commandRows = computed<CommandRow[]>(() => {
  const provider = current.value?.provider
  if (!provider) return []
  const named = name.value || '<name>'
  const addCommand = command(provider, 'add', name.value, url.value)
  const placeholderAdd = command(provider, 'add', 'NAMEPLACEHOLDER', 'https://URLPLACEHOLDER')?.replace('NAMEPLACEHOLDER', '<name>').replace('https://URLPLACEHOLDER', '<url>') ?? ''
  const rows: CommandRow[] = [
    { key: 'add', label: 'Add', command: addCommand, display: addCommand ?? placeholderAdd },
    { key: 'list', label: 'List servers', command: command(provider, 'list'), display: command(provider, 'list') ?? '' },
  ]
  const login = command(provider, 'login', name.value)
  rows.push({ key: 'login', label: `Sign in to ${named}`, command: login, display: login ?? command(provider, 'login', 'NAMEPLACEHOLDER')?.replace('NAMEPLACEHOLDER', '<name>') ?? '' })
  if (provider === 'claude') {
    const remove = command(provider, 'remove', name.value)
    rows.push({ key: 'remove', label: `Remove ${named}`, command: remove, display: remove ?? command(provider, 'remove', 'NAMEPLACEHOLDER')?.replace('NAMEPLACEHOLDER', '<name>') ?? '', enter: false })
  }
  return rows
})

const runNote = ref('')
let noteTimer: ReturnType<typeof setTimeout> | undefined
function flashNote(text: string): void {
  runNote.value = text
  clearTimeout(noteTimer)
  if (text === 'Copied') noteTimer = setTimeout(() => { runNote.value = '' }, 2000)
}

async function runCommand(cmd: string | null, opts?: { enter?: boolean }): Promise<void> {
  if (!cmd || terminal.busy.value) return
  const result = await terminal.run(cmd, opts)
  if (result === 'typed') flashNote('Typed in a separate Ciao terminal session below')
  else if (result === 'copied') flashNote('Copied. Paste it into the terminal.')
  else flashNote(`Could not reach the terminal or the clipboard. Run this yourself: ${cmd}`)
}

async function copyCommand(cmd: string | null): Promise<void> {
  if (cmd && await writeClipboard(cmd)) flashNote('Copied')
}

/** The open chat to sign in through: the first one that reports this server needs a sign-in. */
function signInChat(server: McpNativeServer): string | null {
  return server.connections.find(c => c.state === 'needs_auth')?.chat_id ?? null
}
const signingIn = ref<string | null>(null)
const signInError = ref<string | null>(null)
const SIGN_IN_ERROR = 'The sign-in could not be started. Open the chat and try again.'
/** Raise the action; the chat's own sign-in button starts it on a user gesture. */
async function signInInChat(provider: Provider, serverName: string, chatId: string): Promise<void> {
  if (signingIn.value) return
  const key = menuKey(provider, serverName)
  signingIn.value = key
  signInError.value = null
  try {
    const action = await projectStore.requestMcpAuth(chatId, serverName)
    if (!action) throw new Error('Sign-in action unavailable')
    const { router } = await import('../../router')
    const failure = await router.push(`/chat/${encodeURIComponent(chatId)}?details=mcp`)
    if (failure) throw failure
  } catch {
    signInError.value = key
  } finally {
    signingIn.value = null
  }
}

const openMenu = ref<string | null>(null)
const menuCopied = ref<string | null>(null)
let copiedTimer: ReturnType<typeof setTimeout> | undefined
const moreRefs = new Map<string, HTMLElement>()
function menuKey(provider: Provider, serverName: string): string { return `${provider}:${serverName}` }
function moreKey(server: McpNativeServer): string { return menuKey(current.value?.provider ?? selected.value, server.name) }
function onFocusOut(event: FocusEvent): void {
  const next = event.relatedTarget
  // A null target (Safari does not focus a clicked button) must not close the
  // menu before the click lands; outside clicks are handled separately.
  if (openMenu.value && next instanceof Element && !(event.currentTarget as Element).contains(next)) closeMenu(false)
}
async function signInFromMenu(provider: Provider, serverName: string): Promise<void> {
  closeMenu(true)
  await runCommand(serverCommand(provider, 'login', serverName))
}
function setMoreRef(key: string, el: unknown): void {
  if (el instanceof HTMLElement) moreRefs.set(key, el)
  else moreRefs.delete(key)
}
function toggleMenu(key: string): void {
  if (openMenu.value === key) { closeMenu(true); return }
  openMenu.value = key
  document.addEventListener('click', onOutsideClick, true)
  void nextTick(() => document.querySelector<HTMLElement>('.mcp-menu [role="menuitem"]')?.focus())
}
function closeMenu(refocus: boolean): void {
  const key = openMenu.value
  openMenu.value = null
  document.removeEventListener('click', onOutsideClick, true)
  if (refocus && key) moreRefs.get(key)?.focus()
}
function onOutsideClick(event: MouseEvent): void {
  if (!(event.target instanceof Element) || !event.target.closest('.mcp-more')) closeMenu(false)
}
async function copyServerCommand(provider: Provider, serverName: string): Promise<void> {
  const key = menuKey(provider, serverName)
  const cmd = serverCommand(provider, provider === 'claude' ? 'remove' : 'logout', serverName)
  const copied = cmd ? await writeClipboard(cmd) : false
  closeMenu(true)
  if (!copied) return
  menuCopied.value = key
  clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => { menuCopied.value = null }, 2000)
}
onBeforeUnmount(() => {
  document.removeEventListener('click', onOutsideClick, true)
  clearTimeout(noteTimer)
  clearTimeout(copiedTimer)
})

const toggling = ref<string | null>(null)
const toggleNote = ref('')
const toggleError = ref('')
/** The provider the note or error belongs to, so a result that lands after a tab switch stays on its own tab. */
const toggleFor = ref<Provider | null>(null)
function isOn(server: McpNativeServer): boolean { return server.enabled !== false }
function toggleSummary(provider: Provider, result: McpToggleResult): string {
  const where = provider === 'claude' ? `for Claude Code chats in ${result.applies_to}` : `in ${result.applies_to}`
  const pending = result.reload.pending.length
  const running = pending === 0 ? '' : pending === 1
    ? ' A running chat keeps its current connection until reloaded.'
    : ` ${pending} running chats keep their current connection until reloaded.`
  const warning = result.warning ? ` ${result.warning}` : ''
  return `${result.server} is ${result.enabled ? 'on' : 'off'} ${where}.${running}${warning} ${providerLabel(provider)} sessions running outside Ciaobot keep their current state until they restart.`
}
async function toggleServer(provider: Provider, server: McpNativeServer): Promise<void> {
  if (toggling.value) return
  const next = !isOn(server)
  toggling.value = menuKey(provider, server.name)
  toggleFor.value = provider
  toggleNote.value = ''
  toggleError.value = ''
  try {
    toggleNote.value = toggleSummary(provider, await props.mcp.setEnabled(provider, server.name, next))
  } catch (e) {
    toggleError.value = errorMessage(e, `${server.name} could not be turned ${next ? 'on' : 'off'}.`)
  } finally {
    toggling.value = null
  }
}

function manageable(server: McpNativeServer): boolean {
  return validServerName(server.name)
}

/** A claude.ai connector: listed by the account, connected on claude.ai. */
function isClaudeAi(server: McpNativeServer): boolean {
  return server.scope === 'claudeai'
}

/** Not a sign-in owed here: never connected, or its claude.ai sign-in lapsed; either way it is fixed on claude.ai. */
function claudeAiNotConnected(state: McpConnection['state'], server: McpNativeServer): boolean {
  return isClaudeAi(server) && state === 'needs_auth'
}

/** A visible terminal sign-in only where a chat reported a problem; an unchecked row has it in its menu. */
function showSignIn(server: McpNativeServer): boolean {
  return isOn(server) && server.connections.some(c => c.state === 'needs_auth' || c.state === 'failed')
}

function needsAuth(server: McpNativeServer): boolean {
  return isOn(server) && server.connections.some(c => c.state === 'needs_auth')
}

function providerLabel(provider: Provider): string {
  return provider === 'claude' ? 'Claude Code' : 'OpenCode'
}

function scopeLabel(scope: string): string {
  const labels: Record<string, string> = { local: 'this workspace', project: 'project', user: 'all workspaces', global: 'all workspaces', account: 'account', claudeai: 'claude.ai', provider: 'provider' }
  return labels[scope] || scope
}

function stateLabel(state: McpConnection['state'], server: McpNativeServer): string {
  if (claudeAiNotConnected(state, server)) return 'not connected'
  return { connected: 'connected', failed: 'connection failed', disabled: 'disabled', needs_auth: 'sign-in required', unknown: 'connection not checked' }[state]
}

function stateClass(state: McpConnection['state'], server: McpNativeServer): string {
  if (claudeAiNotConnected(state, server)) return 'badge--muted'
  return state === 'connected' ? 'badge--success' : state === 'failed' ? 'badge--error' : 'badge--warn'
}
</script>

<style scoped src="./settingsPanels.css"></style>
<style scoped>
.mcp-provider { margin-top: var(--space-4); }
.mcp-provider-heading { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin-bottom: var(--space-2); }
.mcp-provider-heading h3 { margin: 0; font-size: var(--text-base); font-weight: 600; }
.mcp-list-notes .hint { margin: 0 0 var(--space-2); }
.mcp-applies-to code, .mcp-workspace-path { overflow-wrap: anywhere; }
.mcp-list { margin: var(--space-3) 0 0; padding: 0; list-style: none; border-top: 1px solid var(--border); }
.mcp-server { padding: 10px 0; border-bottom: 1px solid var(--border); }
.mcp-server .hint { margin: var(--space-1) 0 0; font-size: var(--text-xs); }
.mcp-server-row { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: var(--space-2) var(--space-4); min-height: var(--touch); }
.mcp-server-main { flex: 1 1 8rem; min-width: 0; }
.mcp-server-heading { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.mcp-server--off .skill-name { color: var(--fg2); font-weight: 500; }
.mcp-server-controls { display: flex; flex: 0 0 auto; align-items: center; gap: var(--space-1) var(--space-2); margin-left: auto; }
.mcp-server-main, .mcp-server-controls { min-height: var(--touch); }
.mcp-server-main { display: flex; flex-direction: column; justify-content: center; }
.mcp-connection { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin-top: var(--space-1); }
.mcp-connection .hint { flex-basis: 100%; }
.mcp-unchecked, .mcp-connection-count { color: var(--fg3); font-size: var(--text-xs); }
.mcp-connection a, .mcp-doc-link { color: var(--accent); font-size: var(--text-sm); }
.mcp-more { position: relative; }
.mcp-menu { position: absolute; z-index: 5; top: 100%; right: 0; min-width: 12rem; margin-top: var(--space-1); padding: var(--space-1); background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius-sm); box-shadow: 0 8px 24px rgb(0 0 0 / 18%); }
.mcp-menuitem { display: block; width: 100%; padding: var(--space-2) var(--space-3); border: 0; background: transparent; color: var(--fg); font: inherit; font-size: var(--text-sm); text-align: left; cursor: pointer; border-radius: var(--radius-sm); }
.mcp-menuitem:hover { background: var(--bg3); }
.mcp-manual { margin-top: var(--space-5); border-top: 1px solid var(--border); }
.mcp-manual > summary { display: flex; align-items: center; min-height: var(--touch); color: var(--fg); font-size: var(--text-sm); font-weight: 600; cursor: pointer; }
.mcp-manual > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.mcp-manual[open] > summary { margin-bottom: var(--space-2); }
.mcp-workspace-path { display: block; margin-bottom: var(--space-3); font-size: var(--text-sm); }
.mcp-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); gap: var(--space-3); }
.mcp-field { min-width: 0; }
.mcp-field label { display: block; margin-bottom: var(--space-1); font-size: var(--text-sm); color: var(--fg2); }
.mcp-field input { box-sizing: border-box; width: 100%; padding: var(--space-2) var(--space-3); background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: var(--radius-sm); font: inherit; }
.mcp-error { margin: var(--space-1) 0 0; font-size: var(--text-sm); color: var(--error); }
.mcp-commands { list-style: none; margin: var(--space-4) 0 0; padding: 0; }
.mcp-commands li { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); padding: var(--space-3) 0; border-bottom: 1px solid var(--border); }
.mcp-command-label { flex-basis: 100%; font-size: var(--text-sm); font-weight: 600; overflow-wrap: anywhere; }
.mcp-commands code { flex: 1 1 18rem; min-width: 0; padding: var(--space-2) var(--space-3); background: var(--bg); border-radius: var(--radius-sm); font-size: var(--text-sm); white-space: pre-wrap; overflow-wrap: anywhere; }
.mcp-command-buttons { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.mcp-field input:focus-visible, .mcp-more-button:focus-visible, .mcp-signin:focus-visible, .mcp-menuitem:focus-visible, .mcp-command-buttons button:focus-visible, .mcp-doc-link:focus-visible, .mcp-connection a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (pointer: coarse) {
  .mcp-connection a, .mcp-doc-link { min-height: var(--touch); display: inline-flex; align-items: center; }
  .mcp-more-button, .mcp-signin, .mcp-menuitem, .mcp-field input, .mcp-command-buttons button { min-height: var(--touch); }
  .mcp-more-button { min-width: var(--touch); }
}
</style>
