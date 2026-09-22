import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { errorMessage } from '../lib/errorMessage'
import type { AgentCliStatus, McpEnvKey, McpProjectServer, McpStatus, McpUsage } from '../lib/types'

/**
 * MCP server configuration for the Settings → MCP tab.
 *
 * Extracted from `SettingsView.vue` so the tab's state machine can be
 * exercised without mounting that view. Everything it touches outside itself
 * arrives through `options`: no Pinia store, no router, no lifecycle hooks and
 * no direct `lib/api` import. `SettingsView` still decides *when* to load
 * (it calls `fetchStatus()`/`fetchUsage()` from its own `onMounted`) and still
 * owns the chat-and-navigate "Add via chat" path, which needs the project
 * store and the router.
 */

/** The `lib/api` surface this composable uses. The real client satisfies it
 *  structurally; a test can pass a plain object of spies. */
export interface McpApiClient {
  get<T>(path: string): Promise<T>
  post<T>(path: string, body?: unknown): Promise<T>
  patch<T>(path: string, body?: unknown): Promise<T>
  del<T>(path: string): Promise<T>
}

export interface McpServersOptions {
  api: McpApiClient
  /** Transient success feedback, routed to the app-wide toast by the caller. */
  notifySaved(body: string, title?: string): void
  /** Failure feedback. Never `alert` — see the note on SettingsView's own. */
  notifyFailed(title: string, detail: string): void
  /** Destructive confirmation for "Delete server". */
  confirmDelete(name: string): Promise<boolean>
}

export type McpEditDraft = {
  transport: string
  url: string
  command: string
  argsText: string
}

export type McpEnvKeyView = McpEnvKey & { hint?: string }

/** Well-known secrets when the status API has not returned env_keys yet. */
const MCP_DEFAULT_ENV_KEYS: Record<string, { key: string; hint: string }> = {
  n8n_mcp: {
    key: 'N8N_MCP_TOKEN',
    hint: 'Bearer token for your n8n MCP HTTP endpoint.',
  },
  notion: {
    key: 'NOTION_TOKEN',
    hint: 'Notion internal integration secret.',
  },
}

/** Fallback tool list for the embedded FastMCP server, used until (or unless)
 *  `/api/mcp/status` reports the live one. */
const EMBEDDED_TOOLS_FALLBACK = [
  'context_get', 'vault_search', 'projects_list', 'project_get', 'project',
  'chats_list', 'chat_get', 'chat_create', 'chat_send',
  'chat_continue', 'chat_retry', 'chat_handover', 'chat_archive', 'chat_delete',
  'schedules_list', 'schedule', 'schedule_action',
  'file_surface',
  'project_action',
]

export interface McpServersController {
  status: Ref<McpStatus | null>
  usage: Ref<McpUsage | null>
  usageLoaded: Ref<boolean>
  usageError: Ref<string>
  agentStatus: Ref<AgentCliStatus | null>
  embeddedTools: ComputedRef<string[]>
  fastMcpEnabled: Ref<boolean>
  showAddServer: Ref<boolean>
  addingServer: Ref<boolean>
  addServerResult: Ref<string>
  addServerError: Ref<boolean>
  newName: Ref<string>
  newTransport: Ref<'http' | 'stdio'>
  newUrl: Ref<string>
  newCommand: Ref<string>
  envInputs: Ref<Record<string, string>>
  envSaving: Ref<boolean>
  envResult: Ref<string>
  envError: Ref<boolean>
  envResultServer: Ref<string>
  serverSaving: Ref<string>
  serverResult: Ref<string>
  serverError: Ref<boolean>
  serverResultName: Ref<string>
  serverTools: Ref<Record<string, string[]>>
  toolsLoading: Ref<Record<string, boolean>>
  toolsError: Ref<Record<string, string>>
  fetchStatus(): Promise<void>
  fetchUsage(): Promise<void>
  fetchAgentStatus(): Promise<void>
  toggleAddServer(): void
  isExpanded(name: string): boolean
  toggleServer(name: string): void
  editDraft(srv: McpProjectServer): McpEditDraft
  setEditField(name: string, field: 'url' | 'command' | 'argsText', value: string): void
  editDirty(srv: McpProjectServer): boolean
  envKeysFor(srv: McpProjectServer): McpEnvKeyView[]
  hasEnvEdits(srv: McpProjectServer): boolean
  saveEnvKeys(srv: McpProjectServer): Promise<void>
  saveServer(srv: McpProjectServer): Promise<void>
  refreshServerTools(srv: McpProjectServer): Promise<void>
  saveFastMcpToggle(): void
  addCustomServer(): Promise<void>
  deleteCustomServer(name: string): Promise<void>
}

export function useMcpServers(options: McpServersOptions): McpServersController {
  const { api, notifySaved, notifyFailed, confirmDelete } = options

  const status = ref<McpStatus | null>(null)
  const usage = ref<McpUsage | null>(null)
  const usageLoaded = ref(false)
  const usageError = ref('')
  const agentStatus = ref<AgentCliStatus | null>(null)

  const showAddServer = ref(false)
  const addingServer = ref(false)
  const addServerResult = ref('')
  const addServerError = ref(false)
  const newName = ref('')
  const newTransport = ref<'http' | 'stdio'>('http')
  const newUrl = ref('')
  const newCommand = ref('')
  const fastMcpEnabled = ref(true)
  const expanded = ref<Record<string, boolean>>({})
  const envInputs = ref<Record<string, string>>({})
  const envSaving = ref(false)
  const envResult = ref('')
  const envError = ref(false)
  const envResultServer = ref('')
  const editDrafts = ref<Record<string, McpEditDraft>>({})
  const serverSaving = ref('')
  const serverResult = ref('')
  const serverError = ref(false)
  const serverResultName = ref('')
  const serverTools = ref<Record<string, string[]>>({})
  const toolsLoading = ref<Record<string, boolean>>({})
  const toolsError = ref<Record<string, string>>({})

  const embeddedTools = computed(() => {
    if (status.value?.tools && status.value.tools.length) {
      return status.value.tools
    }
    return EMBEDDED_TOOLS_FALLBACK
  })

  async function fetchStatus(): Promise<void> {
    try {
      status.value = await api.get<McpStatus>('/api/mcp/status')
    } catch {
      status.value = { enabled: false, bound: false, tool_count: 0 }
    }
  }

  /**
   * `/api/mcp/usage` is deliberately fetched even though no template renders
   * it: the operator reads the endpoint by hand to decide which MCP tools to
   * prune, and the request keeps the route exercised from the app. Do not drop
   * the call because the numbers are not on screen.
   */
  async function fetchUsage(): Promise<void> {
    usageError.value = ''
    try {
      usage.value = await api.get<McpUsage>('/api/mcp/usage')
    } catch (err) {
      usage.value = null
      const message = err instanceof Error ? err.message : String(err)
      // A non-JSON body (the SPA index.html) means the /api/mcp/usage route
      // isn't served yet — the running backend predates it and needs a restart.
      usageError.value = /Unexpected token|not valid JSON|<!DOCTYPE/i.test(message)
        ? 'MCP usage endpoint not available on the running server yet. Restart the Ciaobot service (or ask the operator to Deploy) to enable it.'
        : message || 'Could not load MCP tool usage.'
    } finally {
      usageLoaded.value = true
    }
  }

  async function fetchAgentStatus(): Promise<void> {
    try {
      agentStatus.value = await api.get<AgentCliStatus>('/api/agent/status')
    } catch {
      agentStatus.value = null
    }
  }

  function toggleAddServer(): void {
    showAddServer.value = !showAddServer.value
    addServerResult.value = ''
  }

  function isExpanded(name: string): boolean {
    return !!expanded.value[name]
  }

  function ensureEditDraft(srv: McpProjectServer): void {
    if (!editDrafts.value[srv.name]) {
      editDrafts.value[srv.name] = {
        transport: srv.transport || (srv.url ? 'http' : 'stdio'),
        url: srv.url || '',
        command: srv.command || '',
        argsText: joinArgs(srv.args || []),
      }
    }
  }

  function editDraft(srv: McpProjectServer): McpEditDraft {
    ensureEditDraft(srv)
    return editDrafts.value[srv.name]
  }

  function setEditField(name: string, field: 'url' | 'command' | 'argsText', value: string): void {
    const draft = editDrafts.value[name]
    if (!draft) return
    draft[field] = value
  }

  function editDirty(srv: McpProjectServer): boolean {
    const draft = editDraft(srv)
    const args = joinArgs(srv.args || [])
    if ((draft.transport || srv.transport) === 'http') {
      return draft.url.trim() !== (srv.url || '').trim()
    }
    return draft.command.trim() !== (srv.command || '').trim() || draft.argsText.trim() !== args.trim()
  }

  function toggleServer(name: string): void {
    const next = !expanded.value[name]
    expanded.value[name] = next
    if (next) {
      const srv = status.value?.project_servers?.find((s) => s.name === name)
      if (srv) {
        ensureEditDraft(srv)
        if (!(serverTools.value[name]?.length) && !(srv.tools?.length)) {
          void refreshServerTools(srv)
        }
      }
    }
  }

  function hasEnvEdits(srv: McpProjectServer): boolean {
    return envKeysFor(srv).some((entry) => (envInputs.value[entry.key] || '').length > 0)
  }

  function envKeysFor(srv: McpProjectServer): McpEnvKeyView[] {
    if (srv.env_keys?.length) {
      return srv.env_keys.map((entry) => {
        const fallback = MCP_DEFAULT_ENV_KEYS[srv.name]
        return {
          ...entry,
          hint: fallback?.key === entry.key ? fallback.hint : undefined,
        }
      })
    }
    const fallback = MCP_DEFAULT_ENV_KEYS[srv.name]
    if (!fallback) return []
    return [{
      key: fallback.key,
      configured: false,
      source: 'suggested',
      hint: fallback.hint,
    }]
  }

  function splitArgs(text: string): string[] {
    // Shell-style: `"a b"` stays one argument. Splitting on every whitespace
    // boundary turned one quoted argument into several (keeping the quote
    // characters) and wrote the corrupted array to `.mcp.json` on save, so
    // the server subsequently failed or received different values.
    const out: string[] = []
    const re = /"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g
    let match: RegExpExecArray | null
    while ((match = re.exec(text)) !== null) {
      if (match[1] !== undefined) out.push(match[1].replace(/\\(.)/g, '$1'))
      else out.push(match[2] ?? match[3])
    }
    return out
  }

  /** Inverse of `splitArgs` for display: quote what a re-split must keep whole. */
  function joinArgs(args: string[]): string {
    return args.map((a) => (/[\s"']/.test(a) ? JSON.stringify(a) : a)).join(' ')
  }

  function adoptDraftFrom(res: McpStatus, name: string): void {
    const updated = res.project_servers?.find((s) => s.name === name)
    if (updated) {
      editDrafts.value[name] = {
        transport: updated.transport || (updated.url ? 'http' : 'stdio'),
        url: updated.url || '',
        command: updated.command || '',
        argsText: joinArgs(updated.args || []),
      }
    }
  }

  async function saveEnvKeys(srv: McpProjectServer): Promise<void> {
    const keys: Record<string, string> = {}
    for (const entry of envKeysFor(srv)) {
      const value = envInputs.value[entry.key]
      if (value != null && value.length > 0) {
        keys[entry.key] = value
      }
    }
    if (!Object.keys(keys).length) return
    envSaving.value = true
    envResult.value = ''
    envError.value = false
    envResultServer.value = srv.name
    try {
      const res = await api.post<McpStatus>('/api/mcp/env-keys', { keys, server: srv.name })
      status.value = res
      for (const key of Object.keys(keys)) {
        envInputs.value[key] = ''
      }
      adoptDraftFrom(res, srv.name)
      envResult.value = 'Saved to workspace .env. New chats will pick up the keys.'
      notifySaved(`Saved MCP secrets for ${srv.name}.`)
      setTimeout(() => {
        if (envResultServer.value === srv.name) envResult.value = ''
      }, 3000)
    } catch (e) {
      envError.value = true
      envResult.value = errorMessage(e, 'Failed to save MCP secrets.')
    } finally {
      envSaving.value = false
    }
  }

  async function saveServer(srv: McpProjectServer): Promise<void> {
    const draft = editDraft(srv)
    serverSaving.value = srv.name
    serverResult.value = ''
    serverError.value = false
    serverResultName.value = srv.name
    try {
      const body: Record<string, unknown> = {}
      if ((draft.transport || srv.transport) === 'http') {
        body.url = draft.url.trim()
        body.command = ''
        body.args = []
      } else {
        body.command = draft.command.trim()
        body.args = splitArgs(draft.argsText)
        body.url = ''
      }
      const res = await api.patch<McpStatus>(`/api/mcp/servers/${encodeURIComponent(srv.name)}`, body)
      status.value = res
      adoptDraftFrom(res, srv.name)
      serverResult.value = 'Connection saved to .mcp.json.'
      notifySaved(`Updated MCP server ${srv.name}.`)
      setTimeout(() => {
        if (serverResultName.value === srv.name) serverResult.value = ''
      }, 3000)
    } catch (e) {
      serverError.value = true
      serverResult.value = errorMessage(e, 'Failed to save MCP server.')
    } finally {
      serverSaving.value = ''
    }
  }

  async function refreshServerTools(srv: McpProjectServer): Promise<void> {
    toolsLoading.value[srv.name] = true
    toolsError.value[srv.name] = ''
    try {
      const res = await api.get<{
        ok: boolean
        tools?: string[]
        error?: string
        tools_note?: string
        tools_source?: string
      }>(`/api/mcp/servers/${encodeURIComponent(srv.name)}/tools`)
      const tools = res.tools || []
      serverTools.value[srv.name] = tools
      if (status.value?.project_servers) {
        const target = status.value.project_servers.find((s) => s.name === srv.name)
        if (target) {
          target.tools = tools
          target.tools_source = res.tools_source || (tools.length ? 'probed' : 'none')
          if (res.tools_note) target.tools_note = res.tools_note
        }
      }
      if (!res.ok && res.error) {
        toolsError.value[srv.name] = res.error
      }
    } catch (e) {
      const message = errorMessage(e, 'Could not load tools.')
      toolsError.value[srv.name] = /not available on the running server|Unexpected token|<!DOCTYPE|not valid JSON/i.test(message)
        ? 'MCP tools endpoint not available on the running server yet. Use Settings → Deploy, then restart Ciaobot.'
        : message
    } finally {
      toolsLoading.value[srv.name] = false
    }
  }

  function saveFastMcpToggle(): void {
    notifySaved(fastMcpEnabled.value ? 'Ciaobot FastMCP enabled.' : 'Ciaobot FastMCP disabled.')
  }

  async function addCustomServer(): Promise<void> {
    if (!newName.value.trim()) return
    addingServer.value = true
    addServerResult.value = ''
    addServerError.value = false
    const name = newName.value.trim()
    try {
      const body: Record<string, unknown> = { name }
      if (newTransport.value === 'http') {
        body.url = newUrl.value.trim()
      } else {
        const parts = splitArgs(newCommand.value)
        body.command = parts[0] || ''
        body.args = parts.slice(1)
      }
      const res = await api.post<McpStatus>('/api/mcp/servers', body)
      status.value = res
      newName.value = ''
      newUrl.value = ''
      newCommand.value = ''
      showAddServer.value = false
      expanded.value[name] = true
      adoptDraftFrom(res, name)
      notifySaved(`Added MCP server ${name}.`)
    } catch (e) {
      addServerError.value = true
      addServerResult.value = errorMessage(e, `Failed to add MCP server`)
    } finally {
      addingServer.value = false
    }
  }

  async function deleteCustomServer(name: string): Promise<void> {
    if (!await confirmDelete(name)) return
    try {
      const res = await api.del<McpStatus>(`/api/mcp/servers/${encodeURIComponent(name)}`)
      status.value = res
      delete editDrafts.value[name]
      delete serverTools.value[name]
      delete toolsError.value[name]
      notifySaved(`Removed MCP server ${name}.`)
    } catch (e) {
      notifyFailed(`Could not delete MCP server ${name}`, errorMessage(e, 'The request failed.'))
    }
  }

  return {
    status,
    usage,
    usageLoaded,
    usageError,
    agentStatus,
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
    fetchStatus,
    fetchUsage,
    fetchAgentStatus,
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
  }
}
