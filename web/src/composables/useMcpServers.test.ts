import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { useMcpServers } from './useMcpServers'
import type { McpStatus } from '../lib/types'
const inventory = (workspace = 'personal'): McpStatus => ({ workspace, agent_root: '/workspace', providers: [], shell: { kind: 'posix', cd_prefix: 'cd -- /workspace && ' } })
describe('native MCP inventory', () => {
  it('scopes reads to the selected workspace', async () => {
    const get = vi.fn().mockResolvedValue(inventory('client a'))
    const mcp = useMcpServers({ api: { get }, workspace: ref('client a') })
    await mcp.fetchStatus()
    expect(get).toHaveBeenCalledWith('/api/mcp/status?workspace=client%20a')
    expect(mcp.status.value?.workspace).toBe('client a')
    expect(mcp.loading.value).toBe(false)
  })
  it('discards late results from another workspace', async () => {
    let finish!: (value: McpStatus) => void
    const get = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const workspace = ref('personal')
    const mcp = useMcpServers({ api: { get }, workspace })
    const request = mcp.fetchStatus()
    workspace.value = 'work'
    finish(inventory())
    await request
    expect(mcp.status.value).toBeNull()
  })
  it('keeps the newest refresh when responses finish out of order', async () => {
    const finishes: Array<(value: McpStatus) => void> = []
    const get = vi.fn().mockImplementation(() => new Promise(resolve => finishes.push(resolve)))
    const mcp = useMcpServers({ api: { get } })
    const first = mcp.fetchStatus()
    const second = mcp.fetchStatus()
    finishes[1]!(inventory('latest'))
    await second
    finishes[0]!(inventory('old'))
    await first
    expect(mcp.status.value?.workspace).toBe('latest')
  })
  it('clears previous data on a failed refresh', async () => {
    const get = vi.fn().mockRejectedValue(new Error('Configuration unavailable'))
    const mcp = useMcpServers({ api: { get } })
    mcp.status.value = inventory()
    await mcp.fetchStatus()
    expect(mcp.status.value).toBeNull()
    expect(mcp.error.value).toContain('Configuration unavailable')
  })
})
