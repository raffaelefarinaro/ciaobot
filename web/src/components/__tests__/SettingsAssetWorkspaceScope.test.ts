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
  '/api/mcp/status': { workspace: 'personal', agent_root: '/workspace/personal', providers: [] },
  '/api/mcp/usage': {},
  '/api/settings/providers': { connections: {} },
  // The home tab mounts the handover card, which reads its own payload.
  '/api/proposals/handover': {
    workspace: 'personal',
    handover: 'off',
    acting_available: true,
    agreement: {
      decided: 0, matched: 0, rate: null, window: 50, min_decisions: 20, threshold: 0.9,
      predicted_accept: 0, accept_matched: 0, predicted_dismiss: 0, dismiss_matched: 0,
      suggest_handover: false,
    },
  },
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
  // The home tab is the bare `/settings` route: it has no tab segment.
  await router.push(tab === 'home' ? '/settings' : `/settings/${tab}`)
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

// The Workspace health card of the home tab, the one the loader belongs to.
function healthCard(host: HTMLElement): HTMLElement {
  const card = Array.from(host.querySelectorAll<HTMLElement>('.card'))
    .find(el => el.querySelector('.section-title')?.textContent?.trim() === 'Workspace health')
  expect(card).toBeTruthy()
  return card!
}

describe('Settings asset tabs are scoped to the active workspace', () => {
  it('loads the agent assets on a direct load of the home tab, so the health card resolves', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'
    vi.mocked(api.get).mockImplementation(async (raw: string) => {
      paths.push(raw)
      if (raw.split('?')[0] === '/api/agent-assets') {
        return {
          subagents: [],
          commands: [],
          health: {
            status: 'warn',
            checks: [{
              id: 'vault-writable',
              title: 'Vault is not writable',
              status: 'warn',
              detail: 'The vault folder is read-only.',
              path: '/srv/ciao/memory-vault',
              action: 'Make the vault folder writable.',
            }],
          },
        } as never
      }
      const path = raw.split('?')[0]
      return (path in responses ? responses[path] : {}) as never
    })

    const { app, host } = await mountSettings('home')
    try {
      expect(paths).toContain('/api/agent-assets?workspace=personal')
      // The card waits on the assets, so a home load that skips them leaves it
      // on the loader for good.
      await vi.waitFor(() => expect(host.querySelector('[aria-label="Scanning workspace health"]')).toBeNull())
      const card = healthCard(host)
      expect(card.querySelector('.badge')?.textContent?.trim()).toBe('warn')
      expect(card.textContent).toContain('Vault is not writable')
    } finally {
      app.unmount()
      host.remove()
    }
  })

  it('refetches the agent assets when the workspace changes on the home tab', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'
    vi.mocked(api.get).mockImplementation(async (raw: string) => {
      paths.push(raw)
      if (raw.split('?')[0] === '/api/agent-assets') {
        // The two workspaces answer with different reports, so the card's
        // content says which root it is describing.
        const work = raw.includes('workspace=work')
        return {
          subagents: [],
          commands: [],
          health: work
            ? {
                status: 'error',
                checks: [{
                  id: 'vault-writable',
                  title: 'Work vault is not writable',
                  status: 'error',
                  detail: 'The work vault is read-only.',
                  path: '/srv/ciao/work-vault',
                  action: 'Make the work vault folder writable.',
                }],
              }
            : { status: 'ok', checks: [] },
        } as never
      }
      const path = raw.split('?')[0]
      return (path in responses ? responses[path] : {}) as never
    })

    const { app, host } = await mountSettings('home')
    try {
      expect(paths).toContain('/api/agent-assets?workspace=personal')
      paths = []
      // The health card describes one workspace's root, so the switch has to
      // reload the assets rather than keep the previous root's report.
      store.activeWorkspace = 'work'
      await nextTick()
      await nextTick()
      await Promise.resolve()
      await nextTick()

      expect(paths).toContain('/api/agent-assets?workspace=work')
      await vi.waitFor(() => expect(healthCard(host).textContent).toContain('Work vault is not writable'))
    } finally {
      app.unmount()
      host.remove()
    }
  })

  it('sends the selected workspace on the four scoped reads', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'
    paths = []

    const { app, router, host } = await mountSettings()
    try {
      expect(paths).toContain('/api/admin/skills?workspace=personal')
      expect(paths).not.toContain('/api/commands?workspace=personal')
      for (const [tab, path] of [['commands', '/api/commands'], ['subagents', '/api/agent-assets'], ['mcp', '/api/mcp/status']]) {
        await router.push(`/settings/${tab}`)
        await nextTick()
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

    const { app, router, host } = await mountSettings()
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
      expect(paths).not.toContain('/api/agent-assets?workspace=work')
      expect(paths).not.toContain('/api/commands?workspace=work')
      expect(paths).not.toContain('/api/mcp/status?workspace=work')
      for (const [tab, path] of [['commands', '/api/commands'], ['subagents', '/api/agent-assets'], ['mcp', '/api/mcp/status']]) {
        await router.push(`/settings/${tab}`)
        await nextTick()
        expect(paths).toContain(`${path}?workspace=work`)
      }
    } finally {
      app.unmount()
      host.remove()
    }
  })

  it('drops a slower asset response from the workspace that was just left', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'
    let releasePersonal: (value: unknown) => void = () => {}
    vi.mocked(api.get).mockImplementation(async (raw: string) => {
      paths.push(raw)
      if (raw === '/api/admin/skills?workspace=personal') {
        return new Promise(resolve => { releasePersonal = resolve }) as never
      }
      if (raw === '/api/admin/skills?workspace=work') {
        return { counts: {}, skills: [{ name: 'work-only', label: 'custom', source: 'workspace', source_type: 'custom', description: '', path: '', content: '', installed_targets: [] }] } as never
      }
      const path = raw.split('?')[0]
      return (path in responses ? responses[path] : {}) as never
    })

    const { app, host } = await mountSettings('skills')
    try {
      store.activeWorkspace = 'work'
      await nextTick()
      await nextTick()
      await Promise.resolve()
      await nextTick()
      await vi.waitFor(() => expect(host.textContent).toContain('work-only'))

      releasePersonal({ counts: {}, skills: [{ name: 'personal-only', label: 'custom', source: 'workspace', source_type: 'custom', description: '', path: '', content: '', installed_targets: [] }] })
      await Promise.resolve()
      await nextTick()
      expect(host.textContent).toContain('work-only')
      expect(host.textContent).not.toContain('personal-only')
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
