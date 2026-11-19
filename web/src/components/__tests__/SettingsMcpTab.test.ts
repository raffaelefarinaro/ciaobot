// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createApp, defineComponent, h, nextTick } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { api } from '../../lib/api'

const providers = {
  connections: {
    claude: {
      name: 'claude', label: 'Claude Code', ok: true, auth: 'ok', command: 'claude auth login',
      mcp_command: 'claude mcp',
    },
    opencode: {
      name: 'opencode', label: 'OpenCode', ok: true, auth: 'ok', command: 'opencode auth login',
      mcp_command: 'opencode mcp',
    },
    missing: {
      name: 'missing', label: 'Missing', ok: false, auth: 'not_installed', command: '',
    },
  },
}

describe('Settings MCP tab', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('shows one button per installed agent, in registry order, and does not read MCP status', async () => {
    setActivePinia(createPinia())
    const get = vi.spyOn(api, 'get').mockImplementation(async (raw: string) => {
      if (raw.split('?')[0] === '/api/settings/providers') return providers as never
      if (raw.split('?')[0] === '/api/workspaces') {
        return { workspaces: [{ name: 'personal', vault_root: 'memory-vault', default_provider: 'claude' }], active: 'personal', provider_options: [] } as never
      }
      return {} as never
    })
    vi.spyOn(api, 'post').mockResolvedValue({} as never)
    vi.spyOn(api, 'patch').mockResolvedValue({} as never)
    vi.spyOn(api, 'del').mockResolvedValue({} as never)

    const { createMemoryHistory, createRouter } = await import('vue-router')
    const { default: SettingsView } = await import('../SettingsView.vue')
    const Stub = defineComponent({ render: () => h('div') })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/settings', component: Stub },
        { path: '/settings/:tab', component: Stub },
      ],
    })
    await router.push('/settings/mcp')
    await router.isReady()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(SettingsView as never)
    app.use(router)
    app.mount(host)
    try {
      await flushPromises()
      await nextTick()
      const labels = [...host.querySelectorAll('button')]
        .map(button => button.textContent?.trim() ?? '')
        .filter(text => text.startsWith('Open MCP settings'))
      expect(labels).toEqual([
        'Open MCP settings in Claude Code',
        'Open MCP settings in OpenCode',
      ])
      expect(host.textContent).toContain('Each agent manages its own MCP servers and sign-ins.')
      expect(get.mock.calls.some(([path]) => String(path).includes('/api/mcp/status'))).toBe(false)
    } finally {
      app.unmount()
      host.remove()
    }
  })
})
