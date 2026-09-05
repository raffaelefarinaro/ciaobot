import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../lib/api'
import { useVaultReviewStore } from './vaultReview'
import type { VaultReviewCandidate, VaultTrashedNote } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const get = vi.mocked(api.get)
const post = vi.mocked(api.post)

function candidate(overrides: Partial<VaultReviewCandidate> = {}): VaultReviewCandidate {
  return {
    candidate_id: 'abc123abc123abc123abc123',
    workspace: 'personal',
    path: 'memory-vault/People/Mo.md',
    content_hash: 'deadbeef',
    signals: ['unlinked'],
    priority: 1,
    evidence: {
      backlinks: [],
      outbound_links: [],
      bridge: false,
      duplicate_group: [],
      last_update: '',
      type: 'note',
      age_days: null,
    },
    status: 'candidate',
    disposition: '',
    deferred_until: '',
    ...overrides,
  }
}

function trashed(overrides: Partial<VaultTrashedNote> = {}): VaultTrashedNote {
  return {
    candidate_id: 'abc123abc123abc123abc123',
    workspace: 'personal',
    original_path: 'memory-vault/People/Mo.md',
    content_hash: 'deadbeef',
    trashed_at: '2026-09-01T00:00:00Z',
    ...overrides,
  }
}

describe('vaultReview store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('loads candidates and the trash inventory for one workspace', async () => {
    get.mockResolvedValue({ candidates: [candidate()], trashed: [trashed()] })
    const store = useVaultReviewStore()

    await store.fetch('personal')

    expect(get).toHaveBeenCalledWith('/api/vault/review?workspace=personal&include=trashed')
    expect(store.candidates).toHaveLength(1)
    expect(store.trashed).toHaveLength(1)
    expect(store.loadedWorkspace).toBe('personal')
  })

  it('ensureLoaded skips the request when the workspace is already loaded', async () => {
    // The endpoint scans every note in the vault three times, and the two
    // callers fire on every mount and every Memory Map view change. `fetch`
    // only joins an in-flight request, so without this guard each tab flip
    // paid for a full rescan.
    get.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const store = useVaultReviewStore()

    await store.ensureLoaded('personal')
    expect(get).toHaveBeenCalledTimes(1)

    await store.ensureLoaded('personal')
    expect(get).toHaveBeenCalledTimes(1)

    // A different workspace is a different queue, and an explicit refresh
    // still forces a real request.
    await store.ensureLoaded('work')
    expect(get).toHaveBeenCalledTimes(2)
    await store.fetch('work', { force: true })
    expect(get).toHaveBeenCalledTimes(3)
  })

  it('drops a stale response when the workspace changed mid-flight', async () => {
    let release!: () => void
    get.mockImplementationOnce(
      () => new Promise(resolve => { release = () => resolve({ candidates: [candidate()], trashed: [] }) }),
    )
    get.mockResolvedValue({ candidates: [], trashed: [] })
    const store = useVaultReviewStore()

    const first = store.fetch('personal')
    await store.fetch('work', { force: true })
    release()
    await first

    expect(store.loadedWorkspace).toBe('work')
    expect(store.candidates).toEqual([])
  })

  it('records a keep decision and refreshes the queue', async () => {
    get.mockResolvedValue({ candidates: [], trashed: [] })
    post.mockResolvedValue({ ok: true })
    const store = useVaultReviewStore()

    const ok = await store.decide('personal', 'cid1', 'keep')

    expect(ok).toBe(true)
    expect(post).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'decide',
      candidate_id: 'cid1',
      disposition: 'keep',
    })
  })

  it('sends defer_days only for a defer decision', async () => {
    get.mockResolvedValue({ candidates: [], trashed: [] })
    post.mockResolvedValue({ ok: true })
    const store = useVaultReviewStore()

    await store.decide('personal', 'cid1', 'defer', 14)

    expect(post).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'decide',
      candidate_id: 'cid1',
      disposition: 'defer',
      defer_days: 14,
    })
  })

  it('trashes, restores, and permanently deletes through their own actions', async () => {
    get.mockResolvedValue({ candidates: [], trashed: [] })
    post.mockResolvedValue({ ok: true })
    const store = useVaultReviewStore()

    await store.trash('personal', 'cid1')
    await store.restore('personal', 'cid2')
    await store.remove('personal', 'cid3')

    expect(post).toHaveBeenNthCalledWith(1, '/api/vault/review?workspace=personal', {
      action: 'trash',
      candidate_id: 'cid1',
    })
    expect(post).toHaveBeenNthCalledWith(2, '/api/vault/review?workspace=personal', {
      action: 'restore',
      candidate_id: 'cid2',
    })
    // Permanent deletion carries the exact candidate id as confirmation,
    // which is what the server gates on.
    expect(post).toHaveBeenNthCalledWith(3, '/api/vault/review?workspace=personal', {
      action: 'delete',
      candidate_id: 'cid3',
      confirm: 'cid3',
    })
  })

  it('reports a failed mutation without throwing', async () => {
    post.mockRejectedValue(new Error('candidate changed or no longer exists'))
    const store = useVaultReviewStore()

    const ok = await store.trash('personal', 'cid1')

    expect(ok).toBe(false)
    expect(store.error).toBe('candidate changed or no longer exists')
    expect(store.isBusy('cid1')).toBe(false)
  })
})
