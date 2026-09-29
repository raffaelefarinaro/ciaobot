// @vitest-environment jsdom
// Skills, Subagents, Commands and MCP servers belong to ONE workspace: the
// one selected in the sidebar. The point of these assertions is that the
// workspace name rides every request. Without it the list shows the install
// root while a write lands there too — which is a different root from the one
// a re-rooted install actually runs a workspace's agent from.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createApp, defineComponent, h, nextTick } from 'vue'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'

const responses: Record<string, unknown> = {
  '/api/workspaces': {
    workspaces: [
      { name: 'personal', vault_root: 'memory-vault/personal', default_provider: 'claude' },
      { name: 'work', vault_root: 'memory-vault/work', default_provider: 'claude' },
    ],
    active: 'personal',
    provider_options: [{ value: 'claude', label: 'Anthropic (via Claude Code)' }],
  },
  '/api/settings/routines': { model_options: { anthropic: [] } },
  '/api/admin/skills': { counts: {}, skills: [] },
  '/api/commands': { commands: [], skills: [] },
  '/api/agent-assets': { subagents: [], commands: [], health: { status: 'ok', checks: [] } },
  '/api/mcp/status': { enabled: false, bound: false, tool_count: 0, project_servers: [] },
  '/api/mcp/usage': {},
  '/api/settings/providers': { connections: {} },
}

let paths: string[] = []

beforeEach(() => {
  paths = []
  setActivePinia(createPinia())
  vi.spyOn(api, 'get').mockImplementation(async (raw: string) => {
    paths.push(raw)
    const path = raw.split('?')[0]
    return (path in responses ? responses[path] : {}) as never
  })
  vi.spyOn(api, 'post').mockImplementation(async (raw: string) => {
    paths.push(raw)
    return {} as never
  })
  vi.spyOn(api, 'patch').mockImplementation(async (raw: string) => {
    paths.push(raw)
    return {} as never
  })
  vi.spyOn(api, 'del').mockImplementation(async (raw: string) => {
    paths.push(raw)
    return {} as never
  })
})

afterEach(() => vi.restoreAllMocks())

// Settings is routed, so it is mounted inside a real router rather than
// through a test-only prop.
async function mountSettings(tab = 'skills') {
  const { createMemoryHistory, createRouter } = await import('vue-router')
  const { default: SettingsView } = await import('../SettingsView.vue')
  const Stub = defineComponent({ render: () => h('div') })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Stub },
      { path: '/settings', component: Stub },
      { path: '/settings/:tab', component: Stub },
      { path: '/chat/:id', component: Stub },
    ],
  })
  await router.push(`/settings/${tab}`)
  await router.isReady()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(SettingsView as never)
  app.use(router)
  app.mount(host)
  await nextTick()
  await nextTick()
  await Promise.resolve()
  await nextTick()
  return { app, router, host }
}

describe('Settings asset tabs are scoped to the active workspace', () => {
  it('sends the selected workspace on the four scoped reads', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'
    paths = []

    const { app, host } = await mountSettings()
    try {
      for (const path of ['/api/admin/skills', '/api/commands', '/api/agent-assets', '/api/mcp/status']) {
        expect(paths).toContain(`${path}?workspace=personal`)
      }
    } finally {
      app.unmount()
      host.remove()
    }
  })

  it('refetches the scoped lists when the workspace changes', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'

    const { app, host } = await mountSettings()
    try {
      paths = []
      // Switching the sidebar's workspace switches which agent root these
      // tabs describe; without the refetch the page would keep showing the
      // previous workspace's assets while every write went to the new one.
      store.activeWorkspace = 'work'
      await nextTick()
      await nextTick()
      await Promise.resolve()
      await nextTick()

      expect(paths).toContain('/api/admin/skills?workspace=work')
      expect(paths).toContain('/api/agent-assets?workspace=work')
      expect(paths).toContain('/api/commands?workspace=work')
      expect(paths).toContain('/api/mcp/status?workspace=work')
    } finally {
      app.unmount()
      host.remove()
    }
  })

  it('sends the workspace when creating a slash command', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'work'

    const { app, host } = await mountSettings('commands')
    try {
      // Drive the real form: open it, fill the visible fields and click
      // Create, so the assertion covers the markup a user actually submits
      // rather than an internal function call.
      const root = app._container as HTMLElement
      const find = (pred: (el: Element) => boolean) =>
        [...root.querySelectorAll('input, textarea, button')]
          .find((el) => pred(el)) as HTMLInputElement | undefined

      find((el) => el.textContent?.trim() === 'New command')!.click()
      await nextTick()

      const nameInput = find((el) => el.getAttribute('placeholder')?.includes('summarize-decision') === true)
      const descInput = find((el) => el.getAttribute('placeholder')?.includes('slash command does') === true)
      const promptInput = find((el) => el.tagName === 'TEXTAREA')
      const createButton = find((el) => el.textContent?.trim() === 'Create command')
      expect(nameInput && descInput && promptInput && createButton).toBeTruthy()

      nameInput!.value = 'brief'
      nameInput!.dispatchEvent(new Event('input'))
      descInput!.value = 'Work brief.'
      descInput!.dispatchEvent(new Event('input'))
      promptInput!.value = 'Write it.'
      promptInput!.dispatchEvent(new Event('input'))
      await nextTick()

      const submit = find((el) => el.textContent?.trim() === 'Create command')!
      submit.click()
      await nextTick()
      await Promise.resolve()
      await nextTick()

      const calls = (api.post as unknown as {
        mock: { calls: [string, Record<string, unknown>][] }
      }).mock.calls
      const body = calls.find(([p]) => p === '/api/agent-assets/commands')?.[1]
      expect(body?.workspace).toBe('work')
      expect(body?.name).toBe('brief')
    } finally {
      app.unmount()
      host.remove()
    }
  })
})
