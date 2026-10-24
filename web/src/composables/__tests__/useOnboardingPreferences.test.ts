import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import { useOnboardingPreferences } from '../useOnboardingPreferences'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn() } }))

beforeEach(() => { setActivePinia(createPinia()); vi.resetAllMocks() })

describe('useOnboardingPreferences', () => {

  it('preselects a pending workspace but does not save until explicitly requested', async () => {
    const progress = { revision: 1, schema: 1, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: { studio: { status: 'in_progress', step: 'access_memory' } } }
    vi.mocked(api.get).mockResolvedValueOnce({ workspace: 'studio', handover: 'off', acting_available: true, agreement: {} } as never).mockResolvedValueOnce(progress as never)
    const store = useProjectStore()
    store.updateWorkspace = vi.fn().mockResolvedValue({}) as never
    const pref = useOnboardingPreferences({ kind: 'workspace', name: 'studio' }, 'studio')
    await pref.load()
    expect(pref.draft.memoryHandover).toBe('all')
    expect(store.updateWorkspace).not.toHaveBeenCalled()
    pref.draft.acknowledged = true
    expect(await pref.saveMemoryHandover()).toBe(true)
    expect(store.updateWorkspace).toHaveBeenCalledWith('studio', { memory_handover: 'all' })
  })

  it('keeps the saved setting when it is not a pending new workspace', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ workspace: 'studio', handover: 'dismiss', acting_available: false, agreement: {} } as never).mockResolvedValueOnce({ revision: 1, schema: 1, installation: { status: 'completed', step: 'finish' }, workspaces: { studio: { status: 'completed', step: 'finish' } } } as never)
    const pref = useOnboardingPreferences({ kind: 'workspace', name: 'studio' }, 'studio')
    await pref.load()
    expect(pref.draft.memoryHandover).toBe('dismiss')
    expect(pref.actingAvailable.value).toBe(false)
  })
})
// @vitest-environment jsdom

describe('onboarding memory preferences consent', () => {
  it('cannot save without the acknowledgement', async () => {
    const store = useProjectStore()
    const update = vi.spyOn(store, 'updateWorkspace').mockResolvedValue(undefined as never)
    vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/proposals/handover')
        ? { handover: 'off', acting_available: true }
        : { schema: 1, revision: 1, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} },
    ) as never)
    const prefs = useOnboardingPreferences({ kind: 'installation' }, 'personal')
    await prefs.load()
    // An installation whose own journey is running is eligible too.
    expect(prefs.draft.memoryHandover).toBe('all')
    expect(await prefs.saveMemoryHandover()).toBe(false)
    expect(update).not.toHaveBeenCalled()

    prefs.draft.acknowledged = true
    expect(await prefs.saveMemoryHandover()).toBe(true)
    expect(update).toHaveBeenCalledWith('personal', { memory_handover: 'all' })
  })

  it('keeps a saved non-off choice instead of proposing again', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/proposals/handover')
        ? { handover: 'dismiss', acting_available: true }
        : { schema: 1, revision: 1, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} },
    ) as never)
    const prefs = useOnboardingPreferences({ kind: 'installation' }, 'personal')
    await prefs.load()
    expect(prefs.draft.memoryHandover).toBe('dismiss')
  })

  it('does not propose for a completed journey', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/proposals/handover')
        ? { handover: 'off', acting_available: true }
        : { schema: 1, revision: 1, installation: { status: 'completed', step: 'finish' }, workspaces: {} },
    ) as never)
    const prefs = useOnboardingPreferences({ kind: 'installation' }, 'personal')
    await prefs.load()
    expect(prefs.draft.memoryHandover).toBe('off')
  })
})

describe('onboarding memory preferences failures', () => {
  it('keeps the draft after a failed save', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/proposals/handover')
        ? { handover: 'off', acting_available: true }
        : { schema: 1, revision: 1, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} },
    ) as never)
    const store = useProjectStore()
    store.updateWorkspace = vi.fn().mockRejectedValue(new Error('offline')) as never
    const prefs = useOnboardingPreferences({ kind: 'installation' }, 'personal')
    await prefs.load()
    prefs.draft.acknowledged = true
    expect(await prefs.saveMemoryHandover()).toBe(false)
    expect(prefs.draft.memoryHandover).toBe('all')
    expect(prefs.savedHandover.value).toBe('off')
    expect(prefs.error.value).toContain('offline')
  })

  it('reports an unavailable trusted service without weakening the choice', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/proposals/handover')
        ? { handover: 'off', acting_available: false }
        : { schema: 1, revision: 1, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} },
    ) as never)
    const prefs = useOnboardingPreferences({ kind: 'installation' }, 'personal')
    await prefs.load()
    expect(prefs.actingAvailable.value).toBe(false)
    expect(prefs.draft.memoryHandover).toBe('all')
  })
})
