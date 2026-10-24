import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import { useOnboardingPreferences } from '../useOnboardingPreferences'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn() } }))

describe('useOnboardingPreferences', () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks() })

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
