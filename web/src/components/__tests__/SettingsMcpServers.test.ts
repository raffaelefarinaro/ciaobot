// @vitest-environment jsdom
//
// Settings → MCP, mounted on its own. No SettingsView, no router, no Pinia:
// the panel's only input is a `useMcpServers` controller built over a stub
// API client, which is exactly the boundary the split introduced.

import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import SettingsMcpServers from '../settings/SettingsMcpServers.vue'
import { useMcpServers, type McpApiClient, type McpServersController } from '../../composables/useMcpServers'
import type { McpStatus } from '../../lib/types'

function makeApi(): McpApiClient & Record<string, ReturnType<typeof vi.fn>> {
  return {
    get: vi.fn(async () => ({ ok: true, tools: [] }) as never),
    post: vi.fn(async () => ({}) as never),
    patch: vi.fn(async () => ({}) as never),
    del: vi.fn(async () => ({}) as never),
  }
}

const STATUS: McpStatus = {
  enabled: true,
  bound: true,
  tool_count: 2,
  env_path: '/ws/.env',
  project_servers: [
    {
      name: 'notion',
      source: 'project',
      transport: 'http',
      url: 'https://mcp.notion.com/mcp',
      ready: false,
      env_keys: [{ key: 'NOTION_TOKEN', configured: false, source: 'env' }],
    },
  ],
}

function mountPanel(seed?: (mcp: McpServersController) => void) {
  const api = makeApi()
  const mcp = useMcpServers({
    api,
    notifySaved: vi.fn(),
    notifyFailed: vi.fn(),
    confirmDelete: async () => true,
  })
  mcp.status.value = structuredClone(STATUS)
  seed?.(mcp)
  const wrapper = mount(SettingsMcpServers, { props: { mcp } })
  return { wrapper, mcp, api }
}

describe('SettingsMcpServers', () => {
  it('lists project MCP servers without the agent CLI control surface', () => {
    const { wrapper } = mountPanel()
    const names = wrapper.findAll('.skill-name').map(n => n.text())
    expect(names).toEqual(['notion'])
    expect(wrapper.find('#mcp-servers').exists()).toBe(true)
  })

  it('badges a server that is missing its secrets', () => {
    const { wrapper } = mountPanel()
    expect(wrapper.text()).toContain('needs .env')
  })

  it('reports "Add via chat" upward instead of navigating itself', async () => {
    const { wrapper } = mountPanel()
    await wrapper.findAll('.settings-card-header-actions button')[0].trigger('click')
    expect(wrapper.emitted('create-via-chat')).toHaveLength(1)
  })

  it('opens the add form through the controller and posts the typed server', async () => {
    const { wrapper, mcp, api } = mountPanel()
    const buttons = wrapper.findAll('.settings-card-header-actions button')
    await buttons[1].trigger('click')
    expect(mcp.showAddServer.value).toBe(true)

    const panel = wrapper.find('.settings-form-panel')
    await panel.findAll('input')[0].setValue('postgres')
    await panel.findAll('input')[1].setValue('https://db.example/mcp')
    await panel.find('.settings-actions button').trigger('click')

    expect(api.post).toHaveBeenCalledWith('/api/mcp/servers', {
      name: 'postgres',
      url: 'https://db.example/mcp',
    })
  })

  it('expands a server row and shows its editable connection and secret fields', async () => {
    const { wrapper, mcp } = mountPanel()
    await wrapper.findAll('.skill-row')[0].trigger('click')
    await nextTick()
    expect(mcp.isExpanded('notion')).toBe(true)
    expect(wrapper.find('input[aria-label="MCP server URL"]').exists()).toBe(true)
    expect(wrapper.find('input[aria-label="NOTION_TOKEN"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('/ws/.env')
  })

  it('keeps Save secrets and Save connection disabled until something is typed', async () => {
    const { wrapper } = mountPanel(mcp => { mcp.toggleServer('notion') })
    await nextTick()
    const actions = wrapper.find('.mcp-env-block .settings-actions').findAll('button')
    expect(actions[0].attributes('disabled')).toBeDefined()
    expect(actions[1].attributes('disabled')).toBeDefined()

    await wrapper.find('input[aria-label="NOTION_TOKEN"]').setValue('secret')
    await wrapper.find('input[aria-label="MCP server URL"]').setValue('https://elsewhere/mcp')
    const after = wrapper.find('.mcp-env-block .settings-actions').findAll('button')
    expect(after[0].attributes('disabled')).toBeUndefined()
    expect(after[1].attributes('disabled')).toBeUndefined()
  })

  it('deletes through the controller', async () => {
    const { wrapper, api } = mountPanel(mcp => { mcp.toggleServer('notion') })
    await nextTick()
    await wrapper.find('.asset-actions .btn-danger').trigger('click')
    expect(api.del).toHaveBeenCalledWith('/api/mcp/servers/notion')
  })

  // The panel's markup was lifted out of SettingsView's scoped stylesheet, so
  // the shared sheet has to come with it: a scoped rule in the parent never
  // reaches a child's subtree.
  it('carries its own scoped stylesheet', () => {
    const { wrapper } = mountPanel()
    const attrs = Object.keys(wrapper.find('.skill-row').attributes())
    expect(attrs.some(a => a.startsWith('data-v-'))).toBe(true)
  })
})
