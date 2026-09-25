// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import VaultReviewPanel from '../VaultReviewPanel.vue'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useProjectStore } from '../../stores/projects'
import { useFileViewerStore } from '../../stores/fileViewer'
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
    // type · reason · reason · backlinks, one short line.
    const why = wrapper.get('.vr-why').text()
    expect(why).toContain('note')
    expect(why).toContain('nothing links to it')
    expect(why).toContain('no date or tags')
    expect(why).toContain('no backlinks')
    wrapper.unmount()
  })

  it('shows an empty state when nothing is flagged', async () => {
    apiGet.mockResolvedValue({ candidates: [], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('Nothing to revisit')
    wrapper.unmount()
  })

  it('opens with a heading and one sentence, with no refresh or how-to disclosure', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.get('h2').text()).toBe('1 to revisit')
    const lede = wrapper.get('.vr-lede').text()
    expect(lede).toContain('Still true marks a note checked today')
    expect(lede).toContain('Retire moves it to Retired, where it can be restored')
    expect(wrapper.find('.vr-how').exists()).toBe(false)
    expect(wrapper.findAll('button').some(b => b.text() === 'Refresh')).toBe(false)
    wrapper.unmount()
  })

  it('keeps the Still true promise qualified, wherever the copy moves', async () => {
    // "Still true" reports honestly when a note has no frontmatter to stamp.
    // Shortening the surface copy may not shorten that into a flat claim.
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(buttonByText(wrapper, 'Still true').attributes('title'))
      .toContain('when it has frontmatter to stamp')
    wrapper.unmount()
  })

  it('gives every row the same neutral choice, with no pink primary', async () => {
    apiGet.mockResolvedValue({
      candidates: [candidate(), candidate({ candidate_id: 'other', path: 'memory-vault/People/Bo.md' })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.findAll('.btn-primary')).toHaveLength(0)
    const actions = wrapper.findAll('.vr-row')[0].findAll('.vr-actions button').map(b => b.text())
    expect(actions).toEqual(['Still true', 'Retire', 'Discuss'])
    expect(wrapper.findAll('.vr-row')[0].find('.vr-actions .mr-link').text()).toBe('Discuss')
    wrapper.unmount()
  })

  it('filters by reason, one chip per reason present, zeros hidden', async () => {
    apiGet.mockResolvedValue({
      candidates: [
        candidate({ candidate_id: 'a', signals: ['unverified'] }),
        candidate({ candidate_id: 'b', path: 'memory-vault/People/Bo.md', signals: ['unverified', 'superseded_language'] }),
        candidate({ candidate_id: 'c', path: 'memory-vault/People/Cy.md', signals: ['unlinked'] }),
      ],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const chips = wrapper.findAll('.vr-chips button')
    expect(chips.map(c => c.text())).toEqual([
      'All 3', 'Says it was superseded 1', 'Unchecked too long 2', 'Nothing links to it 1',
    ])
    expect(chips.map(c => c.text()).join(' ')).not.toContain('Possible duplicate')
    expect(chips[0].attributes('aria-pressed')).toBe('true')

    await chips[2].trigger('click')
    expect(wrapper.findAll('.vr-row').map(r => r.find('.vr-title').text())).toEqual(['Mo', 'Bo'])
    expect(wrapper.findAll('.vr-chips button')[2].attributes('aria-pressed')).toBe('true')

    await wrapper.findAll('.vr-chips button')[0].trigger('click')
    expect(wrapper.findAll('.vr-row')).toHaveLength(3)
    wrapper.unmount()
  })

  it('names the age and the limit, and opens where the date came from', async () => {
    apiGet.mockResolvedValue({
      candidates: [
        candidate({
          candidate_id: 'fm',
          signals: ['unverified'],
          evidence: {
            ...candidate().evidence,
            type: 'person',
            backlinks: ['a.md', 'b.md', 'c.md'],
            unverified: { age_days: 95, threshold_days: 90, last_verified: '2026-06-22', source: 'frontmatter' },
          },
        }),
        candidate({
          candidate_id: 'mt',
          path: 'memory-vault/People/Juan.md',
          signals: ['unverified'],
          evidence: {
            ...candidate().evidence,
            type: 'person',
            unverified: { age_days: 102, threshold_days: 90, last_verified: '2026-06-15', source: 'mtime' },
          },
        }),
      ],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const [first, second] = wrapper.findAll('.vr-row')
    expect(first.get('.vr-why').text()).toContain('unchecked 3 months (limit 90 days)')
    expect(first.get('.vr-why').text()).toContain('3 backlinks')
    const flag = first.get('.vr-flag')
    expect(flag.attributes('aria-expanded')).toBe('false')
    const box = first.get(`#${flag.attributes('aria-controls')}`)
    expect((box.element as HTMLElement).style.display).toBe('none')

    await flag.trigger('click')
    expect(flag.attributes('aria-expanded')).toBe('true')
    expect((box.element as HTMLElement).style.display).toBe('')
    expect(box.text()).toContain('Last checked 2026-06-22, from the note\'s updated: field.')
    expect(box.text()).toContain('Person notes are due every 90 days.')

    await second.get('.vr-flag').trigger('click')
    expect(second.get('.vr-evidence').text())
      .toContain('No updated: field, so the file\'s modified date is used: 2026-06-15.')
    wrapper.unmount()
  })

  it('quotes the superseded line with its neighbours and opens the note at it', async () => {
    apiGet.mockResolvedValue({
      candidates: [candidate({
        signals: ['superseded_language'],
        evidence: {
          ...candidate().evidence,
          type: 'project',
          superseded: {
            line: 7,
            text: 'Scope change: order numbers moved to their own page.',
            match: 'moved to',
            where: 'lead',
            before: { line: 6, text: '# LVMH P6 OCR trials' },
            after: { line: 8, text: 'Sources: visit 8 Sep.' },
          },
        },
      })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const viewer = useFileViewerStore()
    const open = vi.spyOn(viewer, 'open').mockResolvedValue(true)

    expect(wrapper.get('.vr-why').text()).toContain('says it was superseded')
    await wrapper.get('.vr-flag').trigger('click')
    const box = wrapper.get('.vr-evidence')
    expect(box.text()).toContain('Where it says so · lead paragraph, line 7')
    const lines = box.findAll('.mr-box-line')
    expect(lines.map(l => l.get('.mr-box-num').text())).toEqual(['6', '7', '8'])
    expect(lines[1].classes()).toContain('mr-box-line--hit')
    expect(lines[1].get('mark').text()).toBe('moved to')
    expect(box.text()).toContain('Only the frontmatter and the opening paragraph count.')

    await box.findAll('button').find(b => b.text() === 'Open at line 7')!.trigger('click')
    expect(open).toHaveBeenCalledWith('memory-vault/People/Mo.md', 7)
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

    expect(wrapper.text()).toContain('1 retired note')
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

  it('shows the server excerpt inline and opens the note from its path', async () => {
    apiGet.mockResolvedValue({
      candidates: [candidate({ evidence: { ...candidate().evidence, excerpt: 'Product owner on the OEM side.' } })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const open = vi.spyOn(useFileViewerStore(), 'open').mockResolvedValue(true)

    expect(wrapper.get('.vr-excerpt-inline').text()).toBe('Product owner on the OEM side.')
    await wrapper.get('.vr-path').trigger('click')
    expect(open).toHaveBeenCalledWith('memory-vault/People/Mo.md')
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

    await buttonByText(wrapper, 'Discuss').trigger('click')
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

    await buttonByText(wrapper, 'Discuss').trigger('click')
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

    await buttonByText(wrapper, 'Discuss').trigger('click')
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

    await useVaultReviewStore().fetch('personal', { force: true })
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
