import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useOnboardingStore } from '../onboarding'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn(), post: vi.fn() } }))

const receipt = () => ({ schema: 1 as const, revision: 4, installation: { status: 'in_progress' as const, step: 'location' as const }, workspaces: { studio: { status: 'in_progress' as const, step: 'runtime' as const } } })

beforeEach(() => { vi.resetAllMocks(); setActivePinia(createPinia()) })

describe('onboarding store', () => {
  it('loads once and leaves a missing target unresolved', async () => {
    vi.mocked(api.get).mockResolvedValue(receipt())
    const store = useOnboardingStore()
    await Promise.all([store.load(), store.load()])
    expect(api.get).toHaveBeenCalledOnce()
    expect(store.progress({ kind: 'workspace', name: 'missing' })).toBeNull()
  })

  it('sends the global revision for either target kind', async () => {
    vi.mocked(api.get).mockResolvedValue(receipt())
    vi.mocked(api.patch).mockResolvedValue({ ...receipt(), revision: 5 })
    const store = useOnboardingStore()
    await store.load()
    expect(await store.setStep({ kind: 'installation' }, 'runtime')).toBe(true)
    expect(api.patch).toHaveBeenCalledWith('/api/onboarding', { expected_revision: 4, target: { kind: 'installation' }, step: 'runtime' })
    expect(await store.setStep({ kind: 'workspace', name: 'studio' }, 'care')).toBe(true)
    expect(api.patch).toHaveBeenLastCalledWith('/api/onboarding', { expected_revision: 5, target: { kind: 'workspace', name: 'studio' }, step: 'care' })
  })

  it('reloads a conflict without replaying the write', async () => {
    vi.mocked(api.get).mockResolvedValueOnce(receipt()).mockResolvedValueOnce({ ...receipt(), revision: 8 })
    vi.mocked(api.patch).mockRejectedValue({ status: 409 })
    const store = useOnboardingStore()
    await store.load()
    expect(await store.setStep({ kind: 'installation' }, 'runtime')).toBe(false)
    expect(api.patch).toHaveBeenCalledOnce()
    expect(store.conflict).toBe(true)
    expect(store.receipt?.revision).toBe(8)
  })

  it('adopts completion and reset discards a late response', async () => {
    vi.mocked(api.get).mockResolvedValue(receipt())
    vi.mocked(api.post).mockResolvedValue({ ...receipt(), revision: 5, installation: { status: 'completed', step: 'finish' } })
    const store = useOnboardingStore()
    await store.load()
    expect(await store.complete({ kind: 'installation' })).toBe(true)
    expect(store.progress({ kind: 'installation' })?.status).toBe('completed')
    let resolve!: (v: ReturnType<typeof receipt>) => void
    vi.mocked(api.get).mockReturnValueOnce(new Promise(r => { resolve = r }))
    const pending = store.load()
    store.reset()
    resolve(receipt())
    await pending
    expect(store.receipt).toBeNull()
  })

  it('does not report completion after a load failure', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('offline'))
    const store = useOnboardingStore()
    await store.load()
    expect(store.progress({ kind: 'installation' })).toBeNull()
    expect(store.error).toBeTruthy()
  })
})

describe('onboarding store races', () => {
  it('does not roll the receipt back when an older read resolves after a write', async () => {
    const pending: Array<(value: unknown) => void> = []
    vi.mocked(api.get).mockImplementation(() => new Promise(resolve => pending.push(resolve)) as never)
    vi.mocked(api.patch).mockResolvedValue({ ...receipt(), revision: 5 })
    const store = useOnboardingStore()
    const first = store.load()
    pending[0](receipt())
    await first

    const slow = store.load()
    expect(await store.setStep({ kind: 'installation' }, 'runtime')).toBe(true)
    pending[1]({ ...receipt(), revision: 4 })
    await slow

    expect(store.receipt?.revision).toBe(5)
  })

  it('a pre-reset load does not clear the next load flight', async () => {
    const pending: Array<(value: unknown) => void> = []
    vi.mocked(api.get).mockImplementation(() => new Promise(resolve => pending.push(resolve)) as never)
    const store = useOnboardingStore()
    const first = store.load()
    store.reset()
    const second = store.load()
    pending[0](receipt())
    await first
    // The stale flight must not have cleared the current one: a caller asking
    // again joins B instead of starting a third request.
    const third = store.load()
    expect(api.get).toHaveBeenCalledTimes(2)
    pending[1](receipt())
    await Promise.all([second, third])
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(store.receipt?.revision).toBe(4)
  })

  it('a mutation invalidated by reset does not report success', async () => {
    vi.mocked(api.get).mockResolvedValue(receipt())
    let resolvePatch: (value: unknown) => void = () => {}
    vi.mocked(api.patch).mockReturnValue(new Promise(resolve => { resolvePatch = resolve }) as never)
    const store = useOnboardingStore()
    await store.load()
    const saving = store.setStep({ kind: 'installation' }, 'runtime')
    store.reset()
    resolvePatch({ ...receipt(), revision: 5 })
    expect(await saving).toBe(false)
    expect(store.receipt).toBeNull()
  })
})
