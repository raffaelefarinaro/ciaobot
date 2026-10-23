// @vitest-environment jsdom
// A subagent or command row shows one origin badge. The origin label already
// names the scope (Global, Installed), so repeating the raw scope beside it
// read as a duplicate.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createApp, defineComponent, h, nextTick } from 'vue'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'

const responses: Record<string, unknown> = {
  '/api/workspaces': {
    workspaces: [{ name: 'personal', vault_root: 'memory-vault/personal', default_provider: 'claude' }],
    active: 'personal',
    provider_options: [{ value: 'claude', label: 'Anthropic (via Claude Code)' }],
  },
  '/api/settings/routines': { model_options: { anthropic: [] } },
  '/api/admin/skills': { counts: {}, skills: [] },
  '/api/commands': { commands: [], skills: [] },
  '/api/agent-assets': {
    subagents: [{
      name: 'researcher',
      description: 'd',
      source: 'user',
      scope: 'global',
      path: '/u/.claude/agents/researcher.md',
      editable: false,
      vault_path: '',
      content: '',
    }],
    commands: [{
      name: 'deploy',
      description: 'd',
      argument_hint: '',
      source: 'project',
      scope: 'installed',
      path: '/p/.claude/commands/deploy.md',
      editable: false,
      vault_path: '',
      content: '',
    }],
    health: { status: 'ok', checks: [] },
  },
  '/api/mcp/status': { workspace: 'personal', agent_root: '/workspace/personal', providers: [] },
  '/api/mcp/usage': {},
  '/api/settings/providers': { connections: {} },
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.spyOn(api, 'get').mockImplementation(async (raw: string) => {
    const path = raw.split('?')[0]
    return (path in responses ? responses[path] : {}) as never
  })
  vi.spyOn(api, 'post').mockImplementation(async () => ({} as never))
  vi.spyOn(api, 'patch').mockImplementation(async () => ({} as never))
  vi.spyOn(api, 'del').mockImplementation(async () => ({} as never))
})

afterEach(() => vi.restoreAllMocks())

// Settings is routed, so it is mounted inside a real router rather than
// through a test-only prop.
async function mountSettings(tab: string) {
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

function badgesOf(host: HTMLElement, name: string): string[] {
  const row = Array.from(host.querySelectorAll<HTMLElement>('.skill-row'))
    .find(el => {
      const label = el.querySelector('.skill-name, .command-name')?.textContent?.trim()
      return label === name || label === `/${name}`
    })
  expect(row).toBeTruthy()
  return Array.from(row!.querySelectorAll('.skill-badges .badge')).map(b => b.textContent?.trim() ?? '')
}

describe('Settings asset rows show one origin badge', () => {
  it('labels a global subagent once', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'

    const { app, host } = await mountSettings('subagents')
    try {
      expect(badgesOf(host, 'researcher')).toEqual(['Global'])
      expect(Array.from(host.querySelectorAll('.settings-rail-assets .rail-kv')).map(row => row.textContent?.replace(/\s+/g, ' ').trim())).toContain('Global1')
    } finally {
      app.unmount()
      host.remove()
    }
  })

  it('keeps unclassified origins separate from Global', async () => {
    const assets = responses['/api/agent-assets'] as { subagents: Array<Record<string, unknown>> }
    assets.subagents.push({ name: 'mystery', description: 'd', source: 'unknown', scope: 'mystery', path: '/unknown', editable: true, vault_path: '', content: '' })
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'

    const { app, host } = await mountSettings('subagents')
    try {
      const summary = Array.from(host.querySelectorAll('.settings-rail-assets .rail-kv')).map(row => row.textContent?.replace(/\s+/g, ' ').trim())
      expect(summary).toContain('Global1')
      expect(summary).toContain('Other1')
    } finally {
      app.unmount()
      host.remove()
      assets.subagents.pop()
    }
  })

  it('labels an installed command once', async () => {
    const store = useProjectStore()
    await store.fetchWorkspaces()
    store.activeWorkspace = 'personal'

    const { app, host } = await mountSettings('commands')
    try {
      expect(badgesOf(host, 'deploy')).toEqual(['Installed'])
      expect(Array.from(host.querySelectorAll('.settings-rail-assets .rail-kv')).map(row => row.textContent?.replace(/\s+/g, ' ').trim())).toContain('Installed1')
    } finally {
      app.unmount()
      host.remove()
    }
  })
})
