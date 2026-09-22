// The point of this file: every assertion below runs against the composable
// alone. Nothing here mounts SettingsView (or SettingsMcpServers), creates a
// Pinia store or a router — the MCP tab's status, edit drafts, secrets, tool
// probes and the add/delete paths are all reachable from plain refs and a
// stub API client.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMcpServers, type McpApiClient, type McpServersOptions } from './useMcpServers'
import type { McpProjectServer, McpStatus } from '../lib/types'

function server(over: Partial<McpProjectServer> = {}): McpProjectServer {
  return { name: 'notion', source: 'project', transport: 'http', url: 'https://mcp.notion.com/mcp', ...over }
}

function status(over: Partial<McpStatus> = {}): McpStatus {
  return { enabled: true, bound: true, tool_count: 3, project_servers: [server()], ...over }
}

function makeApi() {
  return {
    get: vi.fn(async () => ({}) as never),
    post: vi.fn(async () => ({}) as never),
    patch: vi.fn(async () => ({}) as never),
    del: vi.fn(async () => ({}) as never),
  } satisfies McpApiClient & Record<string, unknown>
}

function make(overrides: Partial<McpServersOptions> = {}) {
  const api = overrides.api ?? makeApi()
  const notifySaved = vi.fn()
  const notifyFailed = vi.fn()
  const confirmDelete = vi.fn(async () => true)
  const mcp = useMcpServers({ api, notifySaved, notifyFailed, confirmDelete, ...overrides })
  return { mcp, api: api as ReturnType<typeof makeApi>, notifySaved, notifyFailed, confirmDelete }
}

beforeEach(() => {
  vi.useRealTimers()
})

describe('status and usage', () => {
  it('loads the status from /api/mcp/status', async () => {
    const { mcp, api } = make()
    api.get.mockResolvedValueOnce(status() as never)
    await mcp.fetchStatus()
    expect(api.get).toHaveBeenCalledWith('/api/mcp/status')
    expect(mcp.status.value?.project_servers?.[0].name).toBe('notion')
  })

  it('falls back to a disabled status rather than throwing', async () => {
    const { mcp, api } = make()
    api.get.mockRejectedValueOnce(new Error('offline'))
    await mcp.fetchStatus()
    expect(mcp.status.value).toEqual({ enabled: false, bound: false, tool_count: 0 })
  })

  // The usage endpoint is queried even though no template renders the result:
  // the operator reads it by hand to decide which MCP tools to prune.
  it('still requests /api/mcp/usage', async () => {
    const { mcp, api } = make()
    api.get.mockResolvedValueOnce({ total_calls: 2, total_errors: 0, tool_count: 1, tools: [] } as never)
    await mcp.fetchUsage()
    expect(api.get).toHaveBeenCalledWith('/api/mcp/usage')
    expect(mcp.usage.value?.total_calls).toBe(2)
    expect(mcp.usageLoaded.value).toBe(true)
    expect(mcp.usageError.value).toBe('')
  })

  it('explains a non-JSON usage body as a stale backend instead of echoing the parse error', async () => {
    const { mcp, api } = make()
    api.get.mockRejectedValueOnce(new Error('Unexpected token < in JSON at position 0'))
    await mcp.fetchUsage()
    expect(mcp.usage.value).toBeNull()
    expect(mcp.usageError.value).toMatch(/Restart the Ciaobot service/)
  })

  it('reports the embedded tool list from the status, and a fallback before it arrives', async () => {
    const { mcp } = make()
    expect(mcp.embeddedTools.value).toContain('vault_search')
    mcp.status.value = status({ tools: ['only_this'] })
    expect(mcp.embeddedTools.value).toEqual(['only_this'])
  })

  it('loads the agent CLI status from /api/agent/status', async () => {
    const { mcp, api } = make()
    api.get.mockResolvedValueOnce({
      ready: true,
      operations: ['context_get', 'memory_status', 'vault_search'],
      telemetry_path: '/tmp/.runtime/mcp_tool_calls.jsonl',
      version: '0.17.0',
    } as never)
    await mcp.fetchAgentStatus()
    expect(api.get).toHaveBeenCalledWith('/api/agent/status')
    expect(mcp.agentStatus.value?.ready).toBe(true)
    expect(mcp.agentStatus.value?.operations).toContain('vault_search')
    expect(mcp.agentStatus.value?.telemetry_path).toContain('mcp_tool_calls.jsonl')
  })

  it('clears the agent CLI status when the request fails', async () => {
    const { mcp, api } = make()
    mcp.agentStatus.value = { ready: true, operations: [], telemetry_path: '', version: '' }
    api.get.mockRejectedValueOnce(new Error('offline'))
    await mcp.fetchAgentStatus()
    expect(mcp.agentStatus.value).toBeNull()
  })
})

describe('expansion and edit drafts', () => {
  it('seeds a draft from the server and probes its tools the first time it opens', async () => {
    const { mcp, api } = make()
    mcp.status.value = status({ project_servers: [server({ args: ['-y', 'pkg'] })] })
    api.get.mockResolvedValueOnce({ ok: true, tools: ['search'], tools_source: 'probed' } as never)

    expect(mcp.isExpanded('notion')).toBe(false)
    mcp.toggleServer('notion')
    expect(mcp.isExpanded('notion')).toBe(true)
    expect(mcp.editDraft(server()).url).toBe('https://mcp.notion.com/mcp')
    expect(api.get).toHaveBeenCalledWith('/api/mcp/servers/notion/tools')

    // Collapsing and reopening must not re-probe: the tools are cached.
    await Promise.resolve()
    await Promise.resolve()
    expect(mcp.serverTools.value.notion).toEqual(['search'])
    api.get.mockClear()
    mcp.toggleServer('notion')
    mcp.toggleServer('notion')
    expect(api.get).not.toHaveBeenCalled()
  })

  it('derives the transport from the shape when the server does not state one', () => {
    const { mcp } = make()
    const stdio = server({ name: 'local', transport: undefined, url: undefined, command: 'npx', args: ['-y', 'pkg'] })
    expect(mcp.editDraft(stdio).transport).toBe('stdio')
    expect(mcp.editDraft(stdio).argsText).toBe('-y pkg')
  })

  it('tracks dirtiness per transport', () => {
    const { mcp } = make()
    const http = server()
    expect(mcp.editDirty(http)).toBe(false)
    mcp.setEditField('notion', 'url', 'https://elsewhere/mcp')
    expect(mcp.editDirty(http)).toBe(true)

    const stdio = server({ name: 'local', transport: 'stdio', url: undefined, command: 'npx', args: ['-y', 'pkg'] })
    mcp.editDraft(stdio)
    expect(mcp.editDirty(stdio)).toBe(false)
    mcp.setEditField('local', 'argsText', '-y other')
    expect(mcp.editDirty(stdio)).toBe(true)
  })

  it('ignores a field write for a server with no draft', () => {
    const { mcp } = make()
    expect(() => mcp.setEditField('never-opened', 'url', 'x')).not.toThrow()
  })
})

describe('secrets', () => {
  it('uses the reported env keys, annotating the one it has a hint for', () => {
    const { mcp } = make()
    const keys = mcp.envKeysFor(server({ env_keys: [
      { key: 'NOTION_TOKEN', configured: true, source: 'env' },
      { key: 'OTHER', configured: false, source: 'env' },
    ] }))
    expect(keys.map(k => k.key)).toEqual(['NOTION_TOKEN', 'OTHER'])
    expect(keys[0].hint).toMatch(/Notion internal integration secret/)
    expect(keys[1].hint).toBeUndefined()
  })

  it('suggests the well-known key when the status reports none', () => {
    const { mcp } = make()
    expect(mcp.envKeysFor(server({ name: 'n8n_mcp' }))).toEqual([
      { key: 'N8N_MCP_TOKEN', configured: false, source: 'suggested', hint: expect.any(String) },
    ])
    expect(mcp.envKeysFor(server({ name: 'unknown-server' }))).toEqual([])
  })

  it('only counts a non-empty paste as an edit', () => {
    const { mcp } = make()
    const srv = server()
    expect(mcp.hasEnvEdits(srv)).toBe(false)
    mcp.envInputs.value.NOTION_TOKEN = ''
    expect(mcp.hasEnvEdits(srv)).toBe(false)
    mcp.envInputs.value.NOTION_TOKEN = 'secret'
    expect(mcp.hasEnvEdits(srv)).toBe(true)
  })

  it('saves only the filled keys, clears them, and re-seeds the draft from the response', async () => {
    const { mcp, api, notifySaved } = make()
    const srv = server({ env_keys: [
      { key: 'NOTION_TOKEN', configured: false, source: 'env' },
      { key: 'UNTOUCHED', configured: false, source: 'env' },
    ] })
    mcp.status.value = status({ project_servers: [srv] })
    mcp.envInputs.value.NOTION_TOKEN = 'secret'
    api.post.mockResolvedValueOnce(status({ project_servers: [server({ url: 'https://new/mcp' })] }) as never)

    await mcp.saveEnvKeys(srv)

    expect(api.post).toHaveBeenCalledWith('/api/mcp/env-keys', { keys: { NOTION_TOKEN: 'secret' }, server: 'notion' })
    expect(mcp.envInputs.value.NOTION_TOKEN).toBe('')
    expect(mcp.editDraft(server()).url).toBe('https://new/mcp')
    expect(mcp.envResult.value).toMatch(/Saved to workspace \.env/)
    expect(notifySaved).toHaveBeenCalledWith('Saved MCP secrets for notion.')
  })

  it('does not call the API when nothing was typed', async () => {
    const { mcp, api } = make()
    await mcp.saveEnvKeys(server())
    expect(api.post).not.toHaveBeenCalled()
  })

  it('surfaces a save failure against that server', async () => {
    const { mcp, api } = make()
    mcp.envInputs.value.NOTION_TOKEN = 'secret'
    api.post.mockRejectedValueOnce(new Error('nope'))
    await mcp.saveEnvKeys(server())
    expect(mcp.envError.value).toBe(true)
    expect(mcp.envResultServer.value).toBe('notion')
    expect(mcp.envSaving.value).toBe(false)
  })
})

describe('connection edits', () => {
  it('sends the URL and blanks the command for an http server', async () => {
    const { mcp, api, notifySaved } = make()
    const srv = server()
    mcp.editDraft(srv)
    mcp.setEditField('notion', 'url', '  https://elsewhere/mcp  ')
    api.patch.mockResolvedValueOnce(status() as never)
    await mcp.saveServer(srv)
    expect(api.patch).toHaveBeenCalledWith('/api/mcp/servers/notion', {
      url: 'https://elsewhere/mcp', command: '', args: [],
    })
    expect(notifySaved).toHaveBeenCalledWith('Updated MCP server notion.')
    expect(mcp.serverSaving.value).toBe('')
  })

  it('splits the args and blanks the URL for a stdio server', async () => {
    const { mcp, api } = make()
    const srv = server({ name: 'local', transport: 'stdio', url: undefined, command: 'npx' })
    mcp.editDraft(srv)
    mcp.setEditField('local', 'argsText', '  -y   @scope/pkg  ')
    api.patch.mockResolvedValueOnce(status() as never)
    await mcp.saveServer(srv)
    expect(api.patch).toHaveBeenCalledWith('/api/mcp/servers/local', {
      command: 'npx', args: ['-y', '@scope/pkg'], url: '',
    })
  })

  it('keeps quoted args whole across display and save', async () => {
    // Splitting on every whitespace boundary turned `--header
    // "Authorization: Bearer …"` into three arguments (keeping the quote
    // characters) and wrote the corrupted array to `.mcp.json` on save.
    const { mcp, api } = make()
    const srv = server({
      name: 'local', transport: 'stdio', url: undefined, command: 'node',
      args: ['server.js', '--header', 'Authorization: Bearer secret'],
    })
    expect(mcp.editDraft(srv).argsText).toBe('server.js --header "Authorization: Bearer secret"')
    api.patch.mockResolvedValueOnce(status() as never)
    await mcp.saveServer(srv)
    expect(api.patch).toHaveBeenCalledWith('/api/mcp/servers/local', {
      command: 'node', args: ['server.js', '--header', 'Authorization: Bearer secret'], url: '',
    })
  })

  it('reports a failed connection save without clearing the row', async () => {
    const { mcp, api } = make()
    api.patch.mockRejectedValueOnce(new Error('bad url'))
    await mcp.saveServer(server())
    expect(mcp.serverError.value).toBe(true)
    expect(mcp.serverResultName.value).toBe('notion')
    expect(mcp.serverResult.value).toContain('bad url')
  })
})

describe('tool probes', () => {
  it('writes the probed tools back onto the row in the status', async () => {
    const { mcp, api } = make()
    mcp.status.value = status()
    api.get.mockResolvedValueOnce({ ok: true, tools: ['a', 'b'], tools_source: 'probed', tools_note: 'note' } as never)
    await mcp.refreshServerTools(server())
    expect(mcp.serverTools.value.notion).toEqual(['a', 'b'])
    expect(mcp.status.value?.project_servers?.[0].tools).toEqual(['a', 'b'])
    expect(mcp.status.value?.project_servers?.[0].tools_source).toBe('probed')
    expect(mcp.toolsLoading.value.notion).toBe(false)
  })

  it('keeps a reported probe error against the row', async () => {
    const { mcp, api } = make()
    api.get.mockResolvedValueOnce({ ok: false, error: 'handshake failed' } as never)
    await mcp.refreshServerTools(server())
    expect(mcp.serverTools.value.notion).toEqual([])
    expect(mcp.toolsError.value.notion).toBe('handshake failed')
  })

  it('translates a missing tools route into deploy advice', async () => {
    const { mcp, api } = make()
    api.get.mockRejectedValueOnce(new Error('Unexpected token <'))
    await mcp.refreshServerTools(server())
    expect(mcp.toolsError.value.notion).toMatch(/Settings → Deploy/)
  })
})

describe('add and delete', () => {
  it('creates an http server, clears the form and opens the new row', async () => {
    const { mcp, api, notifySaved } = make()
    mcp.toggleAddServer()
    expect(mcp.showAddServer.value).toBe(true)
    mcp.newName.value = '  postgres  '
    mcp.newUrl.value = ' https://db/mcp '
    api.post.mockResolvedValueOnce(status({ project_servers: [server({ name: 'postgres', url: 'https://db/mcp' })] }) as never)

    await mcp.addCustomServer()

    expect(api.post).toHaveBeenCalledWith('/api/mcp/servers', { name: 'postgres', url: 'https://db/mcp' })
    expect(mcp.newName.value).toBe('')
    expect(mcp.showAddServer.value).toBe(false)
    expect(mcp.isExpanded('postgres')).toBe(true)
    expect(mcp.editDraft(server({ name: 'postgres', url: 'https://db/mcp' })).url).toBe('https://db/mcp')
    expect(notifySaved).toHaveBeenCalledWith('Added MCP server postgres.')
  })

  it('splits a stdio command line into command and args', async () => {
    const { mcp, api } = make()
    mcp.newName.value = 'local'
    mcp.newTransport.value = 'stdio'
    mcp.newCommand.value = 'npx -y @scope/pkg --flag'
    api.post.mockResolvedValueOnce(status() as never)
    await mcp.addCustomServer()
    expect(api.post).toHaveBeenCalledWith('/api/mcp/servers', {
      name: 'local', command: 'npx', args: ['-y', '@scope/pkg', '--flag'],
    })
  })

  it('does nothing without a name', async () => {
    const { mcp, api } = make()
    await mcp.addCustomServer()
    expect(api.post).not.toHaveBeenCalled()
  })

  it('shows an add failure inline', async () => {
    const { mcp, api } = make()
    mcp.newName.value = 'postgres'
    api.post.mockRejectedValueOnce(new Error('duplicate'))
    await mcp.addCustomServer()
    expect(mcp.addServerError.value).toBe(true)
    expect(mcp.addServerResult.value).toContain('duplicate')
    expect(mcp.addingServer.value).toBe(false)
  })

  it('asks before deleting and drops the cached row state afterwards', async () => {
    const { mcp, api, confirmDelete, notifySaved } = make()
    mcp.editDraft(server())
    mcp.serverTools.value.notion = ['a']
    mcp.toolsError.value.notion = 'stale'
    api.del.mockResolvedValueOnce(status({ project_servers: [] }) as never)

    await mcp.deleteCustomServer('notion')

    expect(confirmDelete).toHaveBeenCalledWith('notion')
    expect(api.del).toHaveBeenCalledWith('/api/mcp/servers/notion')
    expect(mcp.serverTools.value.notion).toBeUndefined()
    expect(mcp.toolsError.value.notion).toBeUndefined()
    expect(notifySaved).toHaveBeenCalledWith('Removed MCP server notion.')
  })

  it('does not delete when the confirmation is declined', async () => {
    const { mcp, api } = make({ })
    const declining = useMcpServers({
      api,
      notifySaved: vi.fn(),
      notifyFailed: vi.fn(),
      confirmDelete: async () => false,
    })
    await declining.deleteCustomServer('notion')
    expect(api.del).not.toHaveBeenCalled()
    expect(mcp.status.value).toBeNull()
  })

  it('reports a failed delete as an error toast', async () => {
    const { mcp, api, notifyFailed } = make()
    api.del.mockRejectedValueOnce(new Error('in use'))
    await mcp.deleteCustomServer('notion')
    expect(notifyFailed).toHaveBeenCalledWith('Could not delete MCP server notion', expect.stringContaining('in use'))
  })

  it('names the FastMCP toggle state in its confirmation', () => {
    const { mcp, notifySaved } = make()
    mcp.saveFastMcpToggle()
    expect(notifySaved).toHaveBeenCalledWith('Ciaobot FastMCP enabled.')
    mcp.fastMcpEnabled.value = false
    mcp.saveFastMcpToggle()
    expect(notifySaved).toHaveBeenCalledWith('Ciaobot FastMCP disabled.')
  })
})
