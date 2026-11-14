import { computed, onBeforeUnmount, onMounted, ref, watch, type ComputedRef } from 'vue'
import { api } from '../lib/api'
import { errorMessage } from '../lib/errorMessage'
import { validServerName } from '../lib/shellQuote'
import type { ChatAction, McpConnection, McpNativeServer, McpStatus } from '../lib/types'

type Provider = 'claude' | 'opencode'
type State = McpConnection['state']
export interface ChatMcpRow {
  name: string
  server: McpNativeServer | null
  enabled: boolean | null
  state: State
  error: string
  action: ChatAction | null
}
export interface ChatMcpOptions {
  workspace: () => string
  provider: () => string
  chatId: () => string
  actions: () => ChatAction[]
  archived: () => boolean
}
const states: State[] = ['failed', 'needs_auth', 'unknown', 'connected', 'disabled']
const pending = new Set(['open', 'working', 'waiting', 'failed'])

function cmp(a: string, b: string): number { return a < b ? -1 : a > b ? 1 : 0 }
function scopeRank(provider: Provider, scope: string): number {
  const list = provider === 'claude' ? ['local', 'project', 'user', 'managed'] : ['custom', 'project', 'user']
  const rank = list.indexOf(scope)
  return rank < 0 ? list.length : rank
}
function compareServer(provider: Provider, a: McpNativeServer, b: McpNativeServer): number {
  return scopeRank(provider, a.scope) - scopeRank(provider, b.scope)
    || cmp(a.source, b.source) || cmp(a.scope, b.scope) || cmp(a.kind ?? '', b.kind ?? '')
    || Number(a.enabled !== false) - Number(b.enabled !== false)
    || Number(a.allowed !== false) - Number(b.allowed !== false)
    || cmp(a.applies_to ?? '', b.applies_to ?? '')
}
function selectAction(actions: ChatAction[]): ChatAction | null {
  return [...actions].sort((a, b) => (Number.isFinite(b.created_at) ? b.created_at : 0) - (Number.isFinite(a.created_at) ? a.created_at : 0) || cmp(b.id, a.id))[0] ?? null
}
function validStatus(value: unknown): value is McpStatus {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<McpStatus>
  return typeof candidate.workspace === 'string' && Array.isArray(candidate.providers)
    && candidate.providers.every(item => item && (item.provider === 'claude' || item.provider === 'opencode')
      && typeof item.available === 'boolean' && (item.inventory_state === 'loaded' || item.inventory_state === 'unknown')
      && Array.isArray(item.servers) && item.servers.every(server => server && typeof server.name === 'string'
        && typeof server.scope === 'string' && typeof server.source === 'string' && Array.isArray(server.connections)
        && (server.enabled === undefined || typeof server.enabled === 'boolean')
        && (server.allowed === null || typeof server.allowed === 'boolean')
        && (server.kind === undefined || typeof server.kind === 'string')
        && server.connections.every(connection => connection && typeof connection.chat_id === 'string'
          && states.includes(connection.state) && (connection.error === undefined || typeof connection.error === 'string'))))
}

export function useChatMcpConnections(options: ChatMcpOptions) {
  const status = ref<McpStatus | null>(null)
  const loading = ref(false)
  const error = ref('')
  const generation = ref(0)
  const validated = ref(false)
  const requestedWorkspace = computed(() => options.workspace())
  const rows: ComputedRef<ChatMcpRow[]> = computed(() => {
    const provider = options.provider()
    if (!options.workspace() || !options.chatId() || options.archived() || (provider !== 'claude' && provider !== 'opencode')) return []
    const entries = status.value?.providers.filter(p => p.provider === provider) ?? []
    const trusted = validated.value && entries.length > 0 && entries.every(p => p.available && p.inventory_state === 'loaded')
    const inventory = entries.flatMap(p => p.servers)
    const actionMap = new Map<string, ChatAction[]>()
    for (const action of options.actions()) {
      if (action.kind !== 'mcp_auth' || action.params.provider !== provider || !action.params.server) continue
      const list = actionMap.get(action.params.server) ?? []
      list.push(action)
      actionMap.set(action.params.server, list)
    }
    const names = new Set([...inventory.map(s => s.name), ...actionMap.keys()])
    return [...names].sort(cmp).map(name => {
      const servers = inventory.filter(s => s.name === name).sort((a, b) => compareServer(provider, a, b))
      const server = servers[0] ?? null
      const obs = trusted ? servers.flatMap(s => s.connections).filter(c => c.chat_id === options.chatId()).sort((a, b) =>
        states.indexOf(a.state) - states.indexOf(b.state)
        || Number(!a.error) - Number(!b.error) || cmp(a.error ?? '', b.error ?? ''))[0] : undefined
      const action = selectAction(actionMap.get(name) ?? [])
      return {
        name, server, enabled: server ? server.enabled !== false : null,
        state: server?.enabled === false ? 'disabled' : obs?.state ?? 'unknown',
        error: server?.enabled === false ? '' : obs?.error || (action?.state === 'failed' && action.error ? action.error : obs?.state === 'failed' ? 'Connection failed. Check the provider settings.' : ''),
        action,
      }
    })
  })
  const problemCount = computed(() => rows.value.filter(row =>
    row.enabled !== false && (row.state === 'failed' || row.state === 'needs_auth' || (!!row.action && pending.has(row.action.state)))).length)

  function canStart(row: ChatMcpRow): boolean {
    const current = rows.value.find(candidate => candidate.name === row.name)
    if (!current) return false
    const provider = options.provider()
    const chatId = options.chatId()
    const workspace = options.workspace()
    const hasCurrentAction = options.actions().some(action => action.kind === 'mcp_auth'
      && action.params.provider === provider && action.params.server === current.name)
    return !error.value && !options.archived()
      && (provider === 'claude' || provider === 'opencode') && !!chatId && !!workspace
      && !hasCurrentAction
      && canStartMcpAuth(current, provider, status.value, workspace, chatId, validated.value)
  }

  let inFlight: { generation: number; promise: Promise<void>; trailing: boolean } | null = null
  async function refresh(): Promise<void> {
    const workspace = options.workspace(), provider = options.provider(), chatId = options.chatId()
    if (!workspace || (provider !== 'claude' && provider !== 'opencode') || !chatId || options.archived()) {
      generation.value++
      status.value = null; validated.value = false; loading.value = false; error.value = ''
      return
    }
    const currentFlight = inFlight
    if (currentFlight?.generation === generation.value) {
      currentFlight.trailing = true
      await currentFlight.promise
      return
    }
    const current = generation.value
    loading.value = true; error.value = ''
    const request = async () => {
      try {
        const result = await api.get<McpStatus>(`/api/mcp/status?workspace=${encodeURIComponent(workspace)}`)
        if (generation.value !== current || options.workspace() !== workspace || options.provider() !== provider || options.chatId() !== chatId) return
        if (!validStatus(result) || result.workspace !== workspace) {
          status.value = null
          validated.value = false
          error.value = 'MCP connection inventory is unavailable.'
          return
        }
        status.value = result
        validated.value = true
      } catch (e) {
        if (generation.value === current) { status.value = null; validated.value = false; error.value = errorMessage(e, 'Could not load MCP connections.') }
      } finally {
        if (generation.value === current) loading.value = false
      }
    }
    const promise = request()
    const flight = { generation: current, promise, trailing: false }
    inFlight = flight
    await promise
    if (inFlight === flight) inFlight = null
    if (flight.trailing && generation.value === current) await refresh()
  }
  let timer: ReturnType<typeof setInterval> | undefined
  let actionSignature = ''
  watch([options.workspace, options.provider, options.chatId, options.archived], () => {
    generation.value++
    status.value = null; validated.value = false; error.value = ''
    void refresh()
  }, { immediate: true, flush: 'sync' })
  watch(() => JSON.stringify(options.actions().map(a => [a.id, a.state])), signature => {
    if (actionSignature && signature !== actionSignature) void refresh()
    actionSignature = signature
  })
  onMounted(() => {
    timer = setInterval(() => { if (!document.hidden) void refresh() }, 15_000)
    document.addEventListener('visibilitychange', onVisibility)
  })
  function onVisibility() { if (!document.hidden) void refresh() }
  onBeforeUnmount(() => { if (timer) clearInterval(timer); document.removeEventListener('visibilitychange', onVisibility); generation.value++ })
  return { status, loading, error, rows, problemCount, refresh, validated, requestedWorkspace, canStart }
}

export function canStartMcpAuth(row: ChatMcpRow, provider: Provider, status: McpStatus | null, workspace: string, chatId: string, trusted: boolean): boolean {
  const entries = status?.providers.filter(p => p.provider === provider) ?? []
  return !!workspace && !!chatId && trusted && status?.workspace === workspace && entries.length > 0
    && entries.every(p => p.available && p.inventory_state === 'loaded')
    && !!row.server && row.enabled === true && row.server.allowed !== false
    && row.server.scope !== 'claudeai' && ['http', 'sse', 'remote'].includes(row.server.kind ?? '')
    && validServerName(row.name) && row.state === 'needs_auth' && row.action === null
}
