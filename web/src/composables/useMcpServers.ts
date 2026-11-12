import { ref, type Ref } from 'vue'
import { errorMessage } from '../lib/errorMessage'
import type { McpStatus, McpToggleResult } from '../lib/types'

export interface McpApiClient {
  get<T>(path: string): Promise<T>
  post?<T>(path: string, body?: unknown): Promise<T>
}

export interface McpServersOptions {
  api: McpApiClient
  workspace?: Ref<string | null> | (() => string | null)
}

export interface McpServersController {
  status: Ref<McpStatus | null>
  loading: Ref<boolean>
  error: Ref<string>
  fetchStatus(): Promise<void>
  setEnabled(provider: 'claude' | 'opencode', server: string, enabled: boolean): Promise<McpToggleResult>
}

/** Native CLI inventory for the sidebar's selected workspace, and turning one server on or off. */
export function useMcpServers(options: McpServersOptions): McpServersController {
  const status = ref<McpStatus | null>(null)
  const loading = ref(false)
  const error = ref('')
  let sequence = 0

  function workspace(): string {
    return (typeof options.workspace === 'function' ? options.workspace() : options.workspace?.value) ?? ''
  }

  async function fetchStatus(): Promise<void> {
    const request = ++sequence
    const name = workspace()
    status.value = null
    loading.value = true
    error.value = ''
    try {
      const result = await options.api.get<McpStatus>(`/api/mcp/status${name ? `?workspace=${encodeURIComponent(name)}` : ''}`)
      if (request === sequence && name === workspace()) status.value = result
    } catch (err) {
      if (request === sequence && name === workspace()) error.value = errorMessage(err, 'Could not load MCP servers.')
    } finally {
      if (request === sequence) loading.value = false
    }
  }

  async function setEnabled(provider: 'claude' | 'opencode', server: string, enabled: boolean): Promise<McpToggleResult> {
    if (!options.api.post) throw new Error('Turning MCP servers on or off is not available here.')
    const name = status.value?.workspace || workspace()
    const result = await options.api.post<McpToggleResult>('/api/mcp/servers/enabled', {
      ...(name ? { workspace: name } : {}), provider, server, enabled,
    })
    if (status.value?.workspace === result.workspace) {
      const row = status.value.providers.find(p => p.provider === provider)?.servers.find(s => s.name === server)
      if (row) row.enabled = result.enabled
    }
    return result
  }

  return { status, loading, error, fetchStatus, setEnabled }
}
