// @vitest-environment jsdom

// The Google link helpers of useWorkspaceAdmin and the one rule that keeps a
// stale draft from overwriting a link: saving a workspace never sends gws_profile.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { computed, defineComponent, h } from 'vue'
import type { WorkspacesResponse } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useWorkspaceAdmin, type WorkspaceAdmin } from '../useWorkspaceAdmin'

const state = vi.hoisted(() => ({ workspaces: null as unknown }))

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn((path: string) => Promise.resolve(path === '/api/workspaces' ? state.workspaces : {})),
    post: vi.fn(() => Promise.resolve({})),
    patch: vi.fn(() => Promise.resolve(state.workspaces)),
    del: vi.fn(() => Promise.resolve({})),
  },
}))

function registry(): WorkspacesResponse {
  return {
    workspaces: ['work', 'home'].map((name) => ({
      name,
      vault_root: `${name}/memory-vault`,
      default_provider: 'claude',
      gws_profile: '',
      color: 'pink',
    })),
    active: 'work',
    primary: 'work',
    default_agent_fs_scope: 'workspace',
    provider_options: [{ value: 'claude', label: 'Claude' }],
  } as WorkspacesResponse
}

async function mountAdmin(): Promise<WorkspaceAdmin> {
  let admin!: WorkspaceAdmin
  const Harness = defineComponent({
    setup() {
      admin = useWorkspaceAdmin({
        notifySaved: vi.fn(),
        notifyFailed: vi.fn(),
        gwsProfileOptions: computed(() => []),
        fetchGwsIntegration: () => Promise.resolve(),
      })
      return () => h('div')
    },
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: Harness }] })
  await router.push('/')
  await router.isReady()
  mount(Harness, { global: { plugins: [router] } })
  await admin.fetchWorkspacesList()
  await flushPromises()
  return admin
}

describe('useWorkspaceAdmin Google link', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    state.workspaces = registry()
    useProjectStore().activeWorkspace = 'work'
    const { api } = await import('../../lib/api')
    vi.mocked(api.patch).mockClear()
  })

  it('setWorkspaceGwsLink keeps a dirty draft dirty and a clean form clean', async () => {
    const { api } = await import('../../lib/api')
    const admin = await mountAdmin()
    const work = admin.workspaceForms.value.find((f) => f.name === 'work')!
    const home = admin.workspaceForms.value.find((f) => f.name === 'home')!

    work.color = 'cyan'
    expect(admin.workspaceDirty(work)).toBe(true)
    await admin.setWorkspaceGwsLink('work', 'x')
    expect(vi.mocked(api.patch)).toHaveBeenCalledWith('/api/workspaces/work', { gws_profile: 'x' })
    expect(work.gws_profile).toBe('x')
    expect(work.color).toBe('cyan')
    expect(admin.workspaceDirty(work)).toBe(true)
    // Discarding the colour keeps the link: the baseline carries it too.
    admin.discardWorkspace('work')
    const discarded = admin.workspaceForms.value.find((f) => f.name === 'work')!
    expect(discarded.gws_profile).toBe('x')
    expect(admin.workspaceDirty(discarded)).toBe(false)

    expect(admin.workspaceDirty(home)).toBe(false)
    await admin.setWorkspaceGwsLink('home', 'y')
    expect(home.gws_profile).toBe('y')
    expect(admin.workspaceDirty(home)).toBe(false)
  })

  it('save no longer sends gws_profile', async () => {
    const { api } = await import('../../lib/api')
    const admin = await mountAdmin()
    const work = admin.workspaceForms.value.find((f) => f.name === 'work')!
    work.color = 'cyan'
    await admin.saveWorkspace('work')
    const [path, body] = vi.mocked(api.patch).mock.calls[0]
    expect(path).toBe('/api/workspaces/work')
    expect(body).not.toHaveProperty('gws_profile')
  })
})
