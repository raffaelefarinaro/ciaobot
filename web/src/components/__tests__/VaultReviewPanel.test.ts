// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import VaultReviewPanel from '../VaultReviewPanel.vue'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useProjectStore } from '../../stores/projects'
import { pendingConfirm } from '../../lib/confirm'
import type { ChatInfo, ProjectInfo, VaultReviewCandidate, VaultTrashedNote } from '../../lib/types'

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

function generalProject(): ProjectInfo {
  return {
    project_id: 'p-general',
    name: 'General',
    workspace: 'personal',
    context: '',
    created_at: '2026-09-01T00:00:00Z',
    order: 0,
    vault_folder: '',
    is_auto: true,
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

function buttonByText(wrapper: VueWrapper | DOMWrapper<Element>, text: string) {
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

    expect(apiGet).toHaveBeenCalledWith('/api/vault/review?workspace=personal&include=trashed,cleared')
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

    expect(wrapper.text()).toContain('Nothing to revisit')
    wrapper.unmount()
  })

  it('opens with one sentence and folds the per-button detail into a disclosure', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const lede = wrapper.find('.vr-lede')
    expect(lede.exists()).toBe(true)
    expect(lede.text().length).toBeLessThan(160)

    const how = wrapper.find('.vr-how')
    expect(how.exists()).toBe(true)
    expect(how.attributes('open')).toBeUndefined()
    expect(wrapper.find('.vr-how-summary').text()).toBe('What each choice does')
    wrapper.unmount()
  })

  it('keeps the Still true promise qualified, wherever the copy moves', async () => {
    // "Still true" reports honestly when a note has no frontmatter to stamp.
    // Shortening the surface copy may not shorten that into a flat claim.
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const how = wrapper.find('.vr-how').text()
    expect(how).toContain('no frontmatter')
    expect(how).toContain('nowhere to record')
    // And the button's own tooltip keeps the same condition.
    expect(buttonByText(wrapper, 'Still true').attributes('title'))
      .toContain('when it has frontmatter to stamp')
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
    // The trash is its own tab now; the parent picks the section, so the test
    // mounts the one it is about rather than scrolling past the queue.
    const wrapper = mount(VaultReviewPanel, {
      props: { section: 'trash' },
      global: { plugins: [pinia] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('1 retired note in personal')
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

  it('opens a seeded discussion chat with the note pinned, and decides nothing', async () => {
    // "Talk about it" is the only action here that must NOT touch the queue:
    // the candidate stays flagged until the user picks a disposition, and the
    // prompt is a draft so their own question can replace it before sending.
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    const pinFile = vi.spyOn(projects, 'pinFile').mockImplementation(() => {})
    apiPost.mockClear()

    await buttonByText(wrapper, 'Talk about it').trigger('click')
    await flushPromises()

    expect(createChat).toHaveBeenCalledTimes(1)
    const [projectId, title, seed] = createChat.mock.calls[0]
    expect(projectId).toBe('p-general')
    expect(title).toBe('Link or retire Mo?')
    expect(seed).toContain('memory-vault/People/Mo.md')
    expect(seed).toContain('no other note links to it')
    expect(seed).toContain('I will pick Still true or Retire myself')
    expect(pinFile).toHaveBeenCalledWith('c-new', 'memory-vault/People/Mo.md')
    // No disposition was recorded: the row is still in the queue.
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.findAll('.vr-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('offers a way back from a keep, and hides the section when nothing was cleared', async () => {
    // A keep is suppressed by content hash, so without this the only route
    // back into the queue was editing the note.
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [], cleared: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.find('.vr-cleared').exists()).toBe(false)

    const store = useVaultReviewStore()
    store.cleared = [{
      candidate_id: 'cleared01cleared01cleared',
      workspace: 'personal',
      path: 'memory-vault/People/Mo.md',
      content_hash: 'deadbeef',
      decided_at: '2026-09-18T10:00:00+00:00',
    }]
    await flushPromises()

    expect(wrapper.find('.vr-cleared').exists()).toBe(true)
    expect(wrapper.find('.vr-cleared').text()).toContain('cleared 2026-09-18')
    apiPost.mockClear()
    apiPost.mockResolvedValue({ ok: true, candidates: [], trashed: [], cleared: [] })
    await buttonByText(wrapper, 'Add back').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'reopen',
      candidate_id: 'cleared01cleared01cleared',
    })
    wrapper.unmount()
  })

  it('no longer promises a 30-day retention the engine does not enforce', async () => {
    apiGet.mockResolvedValue({ candidates: [], trashed: [], cleared: [] })
    const wrapper = mount(VaultReviewPanel, {
      props: { section: 'trash' },
      global: { plugins: [pinia] },
    })
    await flushPromises()

    const text = wrapper.text()
    expect(text).not.toContain('30 days')
    expect(text).toContain('nothing is removed on a timer')
    wrapper.unmount()
  })

  it('seeds an unlinked row with a hunt for the notes that should link to it', async () => {
    // The repair for `unlinked` is a link, and a link lives in another note —
    // so the seed sends the agent after those notes instead of only weighing
    // the keep-or-retire question the row asks. `Link fixed` used to claim
    // this repair and perform none of it.
    apiGet.mockResolvedValue({ candidates: [candidate({ signals: ['unlinked'] })], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    vi.spyOn(projects, 'pinFile').mockImplementation(() => {})

    await buttonByText(wrapper, 'Talk about it').trigger('click')
    await flushPromises()

    const [, title, seed] = createChat.mock.calls[0]
    expect(title).toBe('Link or retire Mo?')
    expect(seed).toContain('search the vault for the notes that should link to it')
    // The approval gate leads. Buried mid-paragraph it was one clause among
    // five, and a model that misses it writes links the user never approved —
    // which, because backlinks are counted live, also clears the row.
    expect((seed as string).split('\n\n').at(-1)).toMatch(/^Write nothing until I say so\./)
    expect(seed).toContain('wait for my go-ahead on each before writing it')
    // The note under review is still not the agent's to change.
    expect(seed).toContain('Do not edit, move, or delete the note itself')
    wrapper.unmount()
  })

  it('keeps the seed read-only when nothing flagged the note as unlinked', async () => {
    apiGet.mockResolvedValue({
      candidates: [candidate({ signals: ['weak_provenance'] })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    vi.spyOn(projects, 'pinFile').mockImplementation(() => {})

    await buttonByText(wrapper, 'Talk about it').trigger('click')
    await flushPromises()

    const [, title, seed] = createChat.mock.calls[0]
    expect(title).toBe('Retire Mo?')
    expect(seed).toContain('Do not edit, move, or delete anything')
    expect(seed).not.toContain('should link to it')
    wrapper.unmount()
  })

  it('shows a retryable load error instead of claiming the queue is empty', async () => {
    apiGet
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ candidates: [], trashed: [], cleared: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const alert = wrapper.get('[role="alert"]')
    expect(alert.text()).toContain('Could not load notes to revisit')
    expect(alert.text()).toContain('offline')
    expect(wrapper.text()).not.toContain('Nothing to revisit')

    await alert.get('button').trigger('click')
    await flushPromises()
    expect(apiGet).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('Nothing to revisit')
    wrapper.unmount()
  })

  it('keeps the last successful rows visible when a refresh fails', async () => {
    apiGet
      .mockResolvedValueOnce({ candidates: [candidate()], trashed: [], cleared: [] })
      .mockRejectedValueOnce(new Error('still offline'))
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await buttonByText(wrapper, 'refresh').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.vr-row')).toHaveLength(1)
    expect(wrapper.get('[role="status"]').text()).toContain('Showing the last successful load')
    expect(wrapper.text()).not.toContain('Nothing to revisit')
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

  // ── the "nothing to stamp" notice reaches the user ────────────────────

  it('toasts a keep that cleared the row without stamping the note', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    apiPost.mockResolvedValue({
      ok: true,
      candidates: [],
      trashed: [],
      result: {
        candidate_id: 'after',
        previous_candidate_id: 'abc123abc123abc123abc123',
        stamped: false,
        stamp_status: 'no_frontmatter',
      },
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    const store = useVaultReviewStore()

    await buttonByText(wrapper, 'Still true').trigger('click')
    await flushPromises()

    const toast = projects.toasts.find(t => t.title === 'Nothing to stamp')
    expect(toast?.body).toContain('no frontmatter to stamp')
    expect(toast?.variant).toBe('info')
    // Drained, so a later remount cannot re-announce the same one.
    expect(store.notice).toBe('')
    wrapper.unmount()
  })

  it('drains a notice raised while the panel was unmounted', async () => {
    // The panel is a `v-if` sibling: flipping to Proposals while the POST is
    // in flight stops the watcher before the notice is set, and a lazy
    // watcher registered fresh on remount never fires for the value already
    // sitting in the ref.
    apiGet.mockResolvedValue({ candidates: [], trashed: [] })
    const store = useVaultReviewStore()
    const projects = useProjectStore()
    store.notice = 'The row is cleared, but this note has no frontmatter to stamp.'

    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(projects.toasts.some(t => t.title === 'Nothing to stamp')).toBe(true)
    expect(store.notice).toBe('')
    wrapper.unmount()
  })
})
