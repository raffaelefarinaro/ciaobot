import { ref, type Ref } from 'vue'
import { errorMessage } from '../lib/errorMessage'
import type { McpStatus } from '../lib/types'

export interface McpApiClient {
  get<T>(path: string): Promise<T>
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
}

/** Read-only native CLI inventory for the sidebar's selected workspace. */
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

  return { status, loading, error, fetchStatus }
}
