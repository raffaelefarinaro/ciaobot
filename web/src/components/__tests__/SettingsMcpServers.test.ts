// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsMcpServers from '../settings/SettingsMcpServers.vue'
import { useMcpServers } from '../../composables/useMcpServers'
import type { McpStatus } from '../../lib/types'
function panel(change?: (status: McpStatus) => void) {
  const status: McpStatus = { workspace: 'personal', agent_root: '/workspace/personal', providers: [
    { provider: 'claude', available: true, inventory_state: 'loaded', servers: [
      { name: 'notion', scope: 'project', source: '.mcp.json', allowed: false, connections: [] },
    ], setup: { add: 'cd /workspace/personal && claude mcp add --scope local example --transport http https://example.com/mcp', authenticate: 'claude mcp login example' } },
    { provider: 'opencode', available: true, inventory_state: 'loaded', servers: [
      { name: 'notion', scope: 'project', source: 'opencode.json', allowed: null, connections: [{ chat_id: 'chat-one', state: 'failed', error: 'MCP server connection failed.' }, { chat_id: 'chat-two', state: 'needs_auth' }] },
    ], setup: { add: 'opencode mcp add example --url https://example.com/mcp', authenticate: 'opencode mcp auth example' } },
  ] }
  change?.(status)
  const get = vi.fn().mockResolvedValue(status)
  const mcp = useMcpServers({ api: { get } })
  mcp.status.value = status
  return { wrapper: mount(SettingsMcpServers, { props: { mcp } }), get }
}
describe('Settings MCP native inventory', () => {
  it('shows providers and live connections without inferring readiness', () => {
    const { wrapper } = panel()
    expect(wrapper.findAll('h3').map(row => row.text())).toEqual(['Claude Code', 'OpenCode'])
    expect(wrapper.text()).toContain('blocked in workspace')
    expect(wrapper.text()).toContain('Connection not checked.')
    expect(wrapper.text()).toContain('connection failed')
    expect(wrapper.text()).toContain('sign-in required')
    expect(wrapper.findAll('.badge--success')).toHaveLength(0)
    expect(wrapper.findAll('input')).toHaveLength(0)
    expect(wrapper.findAll('.mcp-connection a').map(link => link.attributes('href'))).toEqual(['/chat/chat-one', '/chat/chat-two'])
  })
  it('provides workspace setup and CLI authentication instructions', () => {
    const { wrapper } = panel()
    expect(wrapper.text()).toContain('/workspace/personal')
    expect(wrapper.findAll('details')).toHaveLength(2)
    expect(wrapper.text()).toContain('claude mcp login example')
    expect(wrapper.text()).toContain('opencode mcp auth example')
  })
  it('distinguishes unreadable configuration from empty inventory', () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.inventory_state = 'unknown'
      status.providers[0]!.servers = []
      status.providers[1]!.servers = []
      status.providers[1]!.available = false
    })
    expect(wrapper.text()).toContain('Some CLI configuration could not be read')
    expect(wrapper.text()).toContain('No MCP servers configured')
    expect(wrapper.text()).toContain('not installed')
  })
  it('refreshes the read-only inventory', async () => {
    const { wrapper, get } = panel()
    await wrapper.get('button').trigger('click')
    expect(get).toHaveBeenCalledWith('/api/mcp/status')
  })
})
