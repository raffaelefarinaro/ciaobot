// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import VaultReviewPanel from '../VaultReviewPanel.vue'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { pendingConfirm } from '../../lib/confirm'
import type { VaultReviewCandidate, VaultTrashedNote } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))

function candidate(overrides: Partial<VaultReviewCandidate> = {}): VaultReviewCandidate {
  return {
    candidate_id: 'abc123abc123abc123abc123',
    workspace: 'personal',
    path: 'memory-vault/People/Mo.md',
    content_hash: 'deadbeef',
    signals: ['unlinked', 'weak_provenance'],
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
    candidate_id: 'def456def456def456def456',
    workspace: 'personal',
    original_path: 'memory-vault/People/Old.md',
    content_hash: 'cafef00d',
    trashed_at: '2026-09-01T00:00:00Z',
    ...overrides,
  }
}

/** Drain both the microtask queue and jsdom's queued DOM tasks. */
async function settle() {
  for (let i = 0; i < 3; i += 1) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await flushPromises()
  }
}

function buttonByText(wrapper: ReturnType<typeof mount>, text: string) {
  const found = wrapper.findAll('button').find(b => b.text() === text)
  if (!found) throw new Error(`button "${text}" not found`)
  return found
}

describe('VaultReviewPanel', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
    apiPost.mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    document.body.innerHTML = ''
    pendingConfirm.value?.resolve(false)
    vi.restoreAllMocks()
  })

  it('renders candidates with plain-language reasons from a mocked GET', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/vault/review?workspace=personal&include=trashed')
    expect(wrapper.findAll('.vr-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('Mo')
    expect(wrapper.text()).toContain('no other note links to it')
    expect(wrapper.text()).toContain('never verified')
    wrapper.unmount()
  })

  it('shows an empty state when nothing is flagged', async () => {
    apiGet.mockResolvedValue({ candidates: [], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('Nothing flagged here')
    wrapper.unmount()
  })

  it('sends keep and trash through their own actions', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate({ candidate_id: 'cid-keep' })], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await buttonByText(wrapper, 'Still true').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'decide',
      candidate_id: 'cid-keep',
      disposition: 'keep',
    })

    await buttonByText(wrapper, 'Retire').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'trash',
      candidate_id: 'cid-keep',
    })
    wrapper.unmount()
  })

  it('renders the trash inventory with restore and a confirmed permanent delete', async () => {
    apiGet.mockResolvedValue({ candidates: [], trashed: [trashed()] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('Trash (1)')
    expect(wrapper.text()).toContain('Old')

    await buttonByText(wrapper, 'Restore').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'restore',
      candidate_id: 'def456def456def456def456',
    })

    // Permanent deletion waits on the explicit confirm first.
    const deleteClicked = buttonByText(wrapper, 'Delete forever').trigger('click')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalledWith(
      '/api/vault/review?workspace=personal',
      expect.objectContaining({ action: 'delete' }),
    )
    pendingConfirm.value?.resolve(true)
    await deleteClicked
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'delete',
      candidate_id: 'def456def456def456def456',
      confirm: 'def456def456def456def456',
    })
    wrapper.unmount()
  })

  it('retries an excerpt that failed, instead of pinning the error', async () => {
    // A cached FAILURE used to be treated like a cached success, so one
    // transient 500 pinned "Could not load" on the row for the life of the
    // panel — the only way out was a full refresh.
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 500 })
      .mockResolvedValueOnce({ ok: true, text: async () => 'the note body' })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const details = wrapper.find('details.vr-excerpt')
    const el = details.element as HTMLDetailsElement
    // jsdom queues its own `toggle` when `open` flips, so drain the task
    // queue and count from a clean slate rather than racing it.
    el.open = true
    await settle()
    expect(wrapper.text()).toContain('Could not load (HTTP 500)')
    fetchMock.mockClear()

    // Reopening a row whose excerpt FAILED must retry.
    await details.trigger('toggle')
    await settle()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('the note body')
    expect(wrapper.text()).not.toContain('Could not load')

    // Reopening one that SUCCEEDED must not.
    await details.trigger('toggle')
    await settle()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    vi.unstubAllGlobals()
    wrapper.unmount()
  })

  it('marks the store busy while a decision is in flight', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const store = useVaultReviewStore()

    let release!: () => void
    apiPost.mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({ ok: true }) }))
    const clicked = buttonByText(wrapper, 'Still true').trigger('click')
    await flushPromises()
    expect(store.isBusy('abc123abc123abc123abc123')).toBe(true)
    release!()
    await clicked
    await flushPromises()
    expect(store.isBusy('abc123abc123abc123abc123')).toBe(false)
    wrapper.unmount()
  })
})
