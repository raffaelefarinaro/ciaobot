// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { computed, effectScope } from 'vue'
import { useWorkspaceAdmin } from '../useWorkspaceAdmin'
import { useProjectStore } from '../../stores/projects'
import type { WorkspaceInfo } from '../../lib/types'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() } }))

function ws(overrides?: WorkspaceInfo['provider_overrides']): WorkspaceInfo {
  return {
    name: 'work',
    vault_root: 'work',
    default_provider: 'claude',
    gws_profile: '',
    ...(overrides ? { provider_overrides: overrides } : {}),
  }
}

function setup(initial: WorkspaceInfo) {
  setActivePinia(createPinia())
  const store = useProjectStore()
  store.activeWorkspace = 'work'
  store.workspaces = [initial]
  store.workspaceProviderOptions = [{ value: 'claude', label: 'Claude' }]
  const update = vi.fn().mockResolvedValue({})
  store.updateWorkspace = update as never
  store.fetchWorkspaces = vi.fn().mockImplementation(async () => ({ workspaces: store.workspaces })) as never
  const scope = effectScope()
  const admin = scope.run(() => useWorkspaceAdmin({
    notifySaved: vi.fn(),
    notifyFailed: vi.fn(),
    gwsProfileOptions: computed(() => []),
    workspaceCustomGwsProfile: () => false,
    fetchGwsIntegration: vi.fn().mockResolvedValue(undefined),
  }))!
  return { admin, update }
}

describe('useWorkspaceAdmin saveWorkspace provider_overrides', () => {
  beforeEach(() => vi.clearAllMocks())

  it('omits provider_overrides when unchanged', async () => {
    const { admin, update } = setup(ws({ claude: { model: 'sonnet' } }))
    await admin.fetchWorkspacesList()
    await admin.saveWorkspace('work')
    expect(update.mock.calls[0][1]).not.toHaveProperty('provider_overrides')
  })

  it('sends the whole map when changed', async () => {
    const { admin, update } = setup(ws({ claude: { model: 'sonnet' } }))
    await admin.fetchWorkspacesList()
    admin.workspaceForms.value[0].provider_overrides = { claude: { model: 'sonnet', thinking: 'high' }, opencode: { model: 'a/b' } }
    await admin.saveWorkspace('work')
    expect(update.mock.calls[0][1].provider_overrides).toEqual({
      claude: { model: 'sonnet', thinking: 'high' },
      opencode: { model: 'a/b' },
    })
  })

  it('sends {} when the map was emptied', async () => {
    const { admin, update } = setup(ws({ claude: { model: 'sonnet' } }))
    await admin.fetchWorkspacesList()
    admin.workspaceForms.value[0].provider_overrides = {}
    await admin.saveWorkspace('work')
    expect(update.mock.calls[0][1].provider_overrides).toEqual({})
  })

  it('a workspace with no stored overrides starts empty and is not dirty', async () => {
    const { admin } = setup(ws())
    await admin.fetchWorkspacesList()
    expect(admin.workspaceForms.value[0].provider_overrides).toEqual({})
    expect(admin.workspaceDirty(admin.workspaceForms.value[0])).toBe(false)
  })
})
